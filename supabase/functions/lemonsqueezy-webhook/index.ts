// PDF2Wordly — Lemon Squeezy webhook (Supabase Edge Function).
// Endpoint: /functions/v1/lemonsqueezy-webhook
//
// Verifies X-Signature (HMAC-SHA256 of the RAW body with
// LEMONSQUEEZY_WEBHOOK_SECRET, timing-safe compare) BEFORE parsing JSON,
// identifies the user via meta.custom_data.supabase_user_id (fallback:
// stored subscription_id), and updates public.profiles. Duplicate
// deliveries are ignored via public.lemonsqueezy_events.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { createHmac } from "node:crypto";

const WEBHOOK_SECRET = Deno.env.get("LEMONSQUEEZY_WEBHOOK_SECRET") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

// Statuses that grant Pro access. Single definition, mirrors the backend.
const PRO_STATUSES = new Set(["active", "on_trial", "trialing"]);

// Lemon Squeezy subscription.status values that mean "no usable access".
const INACTIVE_STATUSES = new Set([
  "cancelled",
  "expired",
  "past_due",
  "unpaid",
  "paused",
  "refunded",
]);

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function errorJson(message: string, status: number): Response {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function pickUserId(payload: Record<string, unknown>): string | null {
  const meta = (payload["meta"] ?? {}) as Record<string, unknown>;
  const custom = (meta["custom_data"] ?? {}) as Record<string, unknown>;
  const direct = custom["supabase_user_id"];
  if (typeof direct === "string" && direct.length > 10) return direct;
  // Some setups nest it one level deeper; accept without trusting blindly
  // (format-checked as UUID below).
  const nested = (custom["custom"] ?? {}) as Record<string, unknown>;
  const nestedId = nested["supabase_user_id"];
  if (typeof nestedId === "string" && nestedId.length > 10) return nestedId;
  return null;
}

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function pickSubscription(payload: Record<string, unknown>): {
  id: string | null;
  status: string | null;
  renewsAt: string | null;
  endsAt: string | null;
  eventUpdatedAt: string | null;
} {
  const data = (payload["data"] ?? {}) as Record<string, unknown>;
  const attrs = (data["attributes"] ?? {}) as Record<string, unknown>;
  const str = (v: unknown) => (typeof v === "string" && v ? v : null);
  return {
    id: str(data["id"]),
    status: str(attrs["status"])?.toLowerCase() ?? null,
    renewsAt: str(attrs["renews_at"]),
    endsAt: str(attrs["ends_at"]),
    eventUpdatedAt: str(attrs["updated_at"]),
  };
}

// Map an LS state to our profile state. Only active/on_trial grant Pro;
// everything else resolves to Free while preserving the raw status and
// subscription id for history/debugging.
function mapToProfile(
  status: string | null,
  subscriptionId: string | null,
  renewsAt: string | null,
  endsAt: string | null,
): {
  plan: "free" | "pro";
  subscription_status: string;
  current_period_end: string | null;
} {
  const normalized = (status ?? "unknown").toLowerCase();
  const periodEnd = renewsAt ?? endsAt ?? null;
  if (PRO_STATUSES.has(normalized)) {
    return {
      plan: "pro",
      subscription_status: normalized === "on_trial" ? "trialing" : "active",
      current_period_end: periodEnd,
    };
  }
  if (INACTIVE_STATUSES.has(normalized) || normalized === "unknown") {
    return { plan: "free", subscription_status: normalized, current_period_end: periodEnd };
  }
  return { plan: "free", subscription_status: normalized, current_period_end: periodEnd };
}

Deno.serve(async (req: Request): Promise<Response> => {
  if (req.method !== "POST") {
    return errorJson("Method not allowed.", 405);
  }
  if (!WEBHOOK_SECRET || !SUPABASE_URL || !SERVICE_ROLE_KEY) {
    console.error("webhook misconfigured: missing secret or Supabase env");
    return errorJson("Webhook not configured.", 500);
  }

  // 1-2. Raw body + signature header.
  const rawBody = await req.text();
  const signature = req.headers.get("x-signature") ?? "";
  if (!signature) {
    console.warn("webhook rejected: missing X-Signature");
    return errorJson("Invalid signature.", 401);
  }

  // 3-5. HMAC-SHA256 + timing-safe compare.
  const expected = createHmac("sha256", WEBHOOK_SECRET).update(rawBody).digest("hex");
  if (!timingSafeEqual(signature, expected)) {
    console.warn("webhook rejected: bad signature");
    return errorJson("Invalid signature.", 401);
  }

  // 6. Parse ONLY after verification.
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(rawBody) as Record<string, unknown>;
  } catch {
    return errorJson("Invalid JSON.", 400);
  }

  const meta = (payload["meta"] ?? {}) as Record<string, unknown>;
  const eventName = String(meta["event_name"] ?? "unknown");
  const webhookId = String(meta["webhook_id"] ?? "");
  const eventId =
    webhookId && eventName !== "unknown" ? `${webhookId}:${eventName}` : "";

  const admin = createClient(SUPABASE_URL, SERVICE_ROLE_KEY);

  // 7. Idempotency: ignore repeat deliveries of the same event.
  if (eventId) {
    const { data: seen } = await admin
      .from("lemonsqueezy_events")
      .select("event_id")
      .eq("event_id", eventId)
      .limit(1);
    if (seen && seen.length > 0) {
      console.info(`webhook deduped: ${eventId}`);
      return new Response(JSON.stringify({ received: true, deduped: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
  }

  const sub = pickSubscription(payload);
  console.info(
    `webhook ${eventName} sub=${sub.id ?? "?"} status=${sub.status ?? "?"}`,
  );

  // order_created carries no subscription state — acknowledge, change nothing.
  if (eventName === "order_created") {
    if (eventId) {
      await admin.from("lemonsqueezy_events").insert({
        event_id: eventId,
        event_name: eventName,
        subscription_id: sub.id,
        user_id: null,
      });
    }
    return new Response(JSON.stringify({ received: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  const knownEvents = new Set([
    "subscription_created",
    "subscription_updated",
    "subscription_cancelled",
    "subscription_resumed",
    "subscription_expired",
    "subscription_paused",
    "subscription_unpaused",
    "subscription_payment_failed",
    "subscription_payment_success",
    "subscription_payment_recovered",
    "subscription_payment_refunded",
    "subscription_plan_changed",
  ]);
  if (!knownEvents.has(eventName)) {
    console.warn(`webhook ignored: unknown event ${eventName}`);
    return new Response(JSON.stringify({ received: true, ignored: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  // 8. Identify the user: primary = custom_data.supabase_user_id (UUID-checked).
  let userId = pickUserId(payload);
  if (userId && !UUID_RE.test(userId)) {
    console.warn("webhook: custom supabase_user_id is not a UUID; ignoring it");
    userId = null;
  }

  // Fallback: stored subscription_id (safe: set only by prior verified webhooks).
  if (!userId && sub.id) {
    const { data: rows } = await admin
      .from("profiles")
      .select("id")
      .eq("subscription_id", sub.id)
      .limit(1);
    if (rows && rows.length > 0) userId = rows[0]["id"] as string;
  }

  if (!userId) {
    console.warn(`webhook ${eventName}: could not identify user; no update made`);
    return errorJson("Could not identify user for this event.", 422);
  }

  // 9. Load current profile for the out-of-order guard.
  const { data: current } = await admin
    .from("profiles")
    .select("subscription_status, current_period_end")
    .eq("id", userId)
    .limit(1);
  const currentRow = current && current.length > 0 ? current[0] : null;

  const mapped = mapToProfile(sub.status, sub.id, sub.renewsAt, sub.endsAt);

  // Out-of-order guard: never regress a stored ACTIVE subscription with a
  // LATER period end to an older terminal snapshot with an earlier one.
  // (Full ordering is impossible: LS does not send monotonic sequence
  // numbers, so last-write-wins otherwise. See report §M.)
  let effective = mapped;
  try {
    if (
      currentRow &&
      String(currentRow["subscription_status"] ?? "").toLowerCase() === "active" &&
      mapped.plan === "free" &&
      currentRow["current_period_end"] &&
      mapped.current_period_end &&
      new Date(String(currentRow["current_period_end"])).getTime() >
        new Date(mapped.current_period_end).getTime()
    ) {
      console.warn(
        `webhook ${eventName}: stale downgrade ignored (stored period end is newer)`,
      );
      effective = {
        plan: "pro",
        subscription_status: "active",
        current_period_end: String(currentRow["current_period_end"]),
      };
    }
  } catch {
    // Date parse failure -> fall through to the mapped state.
  }

  const { error: updateError } = await admin
    .from("profiles")
    .update({
      plan: effective.plan,
      subscription_status: effective.subscription_status,
      subscription_id: sub.id,
      current_period_end: effective.current_period_end,
      updated_at: new Date().toISOString(),
    })
    .eq("id", userId);

  if (updateError) {
    console.error(`webhook ${eventName}: profile update failed: ${updateError.message}`);
    return errorJson("Failed to update profile.", 500);
  }

  if (eventId) {
    await admin.from("lemonsqueezy_events").insert({
      event_id: eventId,
      event_name: eventName,
      subscription_id: sub.id,
      user_id: userId,
    });
  }

  console.info(
    `webhook ${eventName}: user updated to plan=${effective.plan} status=${effective.subscription_status}`,
  );
  return new Response(JSON.stringify({ received: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});

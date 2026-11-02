// ticker.daily Telegram webhook (Cloudflare Worker).
// Receives button taps, checks they came from you, and starts the GitHub decision workflow.
//
// Worker secrets (Settings -> Variables and Secrets):
//   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_SECRET, GITHUB_TOKEN
// Optional plain variable: GITHUB_REPO (default sureshravuri01/ticker.daily)

const ACTIONS = {
  approve: { label: "✅ Approved", toast: "Approved. Logging it now." },
  reject: { label: "❌ Rejected", toast: "Rejected. Logged." },
  regen: { label: "🔄 Regenerating…", toast: "On it. A fresh draft arrives in ~3 minutes." },
};
const POST_ID_RE = /^\d{4}-\d{2}-\d{2}-[a-z0-9-]{1,40}$/;

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ticker.daily bot is running", { status: 200 });
    // Only Telegram knows this secret (set via setWebhook), so forged requests stop here.
    if (!safeEqual(request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "", env.WEBHOOK_SECRET || "")) {
      return new Response("unauthorized", { status: 401 });
    }
    let update;
    try { update = await request.json(); } catch { return new Response("ok"); }
    try {
      if (update.callback_query) await onButton(update.callback_query, env);
      else if (update.message) await onMessage(update.message, env);
    } catch (err) {
      console.log("handler error", err && err.stack);
    }
    return new Response("ok"); // always 200, or Telegram keeps retrying
  },
};

async function onButton(cb, env) {
  const tg = (m, p) => telegram(env, m, p);
  if (String(cb.from.id) !== String(env.TELEGRAM_CHAT_ID)) {
    return tg("answerCallbackQuery", { callback_query_id: cb.id, text: "Not authorized." });
  }
  const [action, postId] = String(cb.data || "").split("|");
  const chat_id = cb.message.chat.id, message_id = cb.message.message_id;

  if (action === "ping") {
    await tg("answerCallbackQuery", { callback_query_id: cb.id, text: "🏓 Pong! Buttons work." });
    return tg("editMessageText", { chat_id, message_id,
      text: "✅ Connected. Button taps reach the Worker and your chat ID is verified." });
  }
  if (action === "noop") return tg("answerCallbackQuery", { callback_query_id: cb.id });
  if (!ACTIONS[action] || !POST_ID_RE.test(postId || "")) {
    return tg("answerCallbackQuery", { callback_query_id: cb.id, text: "Unknown button." });
  }

  const repo = env.GITHUB_REPO || "sureshravuri01/ticker.daily";
  const gh = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/decision.yml/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "ticker-daily-bot",
    },
    body: JSON.stringify({ ref: "main", inputs: { post_id: postId, action } }),
  });
  if (gh.status !== 204) {
    const detail = (await gh.text()).slice(0, 150);
    console.log("github dispatch failed", gh.status, detail);
    return tg("answerCallbackQuery", { callback_query_id: cb.id, show_alert: true,
      text: `Couldn't start the workflow (GitHub ${gh.status}). Check the Worker's GITHUB_TOKEN.` });
  }
  await tg("answerCallbackQuery", { callback_query_id: cb.id, text: ACTIONS[action].toast });
  // Swap the buttons for a status label so the same draft can't be decided twice.
  const when = new Date().toLocaleTimeString("en-US", { timeZone: "America/Los_Angeles", hour: "numeric", minute: "2-digit" });
  return tg("editMessageReplyMarkup", { chat_id, message_id,
    reply_markup: { inline_keyboard: [[{ text: `${ACTIONS[action].label} · ${when}`, callback_data: "noop|x" }]] } });
}

async function onMessage(msg, env) {
  if (String(msg.from && msg.from.id) !== String(env.TELEGRAM_CHAT_ID)) return; // ignore strangers
  return telegram(env, "sendMessage", { chat_id: msg.chat.id,
    text: "👋 I'm the ticker.daily approval bot. Drafts arrive here on weekdays after the close; tap a button to approve, reject or regenerate." });
}

async function telegram(env, method, payload) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  if (!r.ok) console.log("telegram", method, r.status, (await r.text()).slice(0, 200));
  return r;
}

function safeEqual(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

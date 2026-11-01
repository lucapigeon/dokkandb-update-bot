const PREFS = [
  { key: "cards_glb", label: "Carte GLB" },
  { key: "cards_jp", label: "Carte JP" },
  { key: "summons_glb", label: "Summon GLB" },
  { key: "summons_jp", label: "Summon JP" },
  { key: "events_glb", label: "Eventi GLB" },
  { key: "events_jp", label: "Eventi JP" },
  { key: "news_glb", label: "News GLB" },
  { key: "news_jp", label: "News JP" },
];

const ACTIVE_STATUSES = new Set(["member", "administrator", "creator"]);
const GROUP_TYPES = new Set(["group", "supergroup"]);

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function apiAuthorized(request, env) {
  const expected = env.MONITOR_API_SECRET;
  const actual = request.headers.get("authorization") || "";
  return Boolean(expected) && actual === `Bearer ${expected}`;
}

async function telegram(env, method, payload) {
  if (!env.TELEGRAM_BOT_TOKEN) {
    throw new Error("TELEGRAM_BOT_TOKEN non configurato nei Secrets del Worker");
  }

  let response;
  try {
    response = await fetch(
      `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload || {}),
      },
    );
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`Telegram ${method}: errore di rete durante la richiesta (${detail})`);
  }

  const rawBody = await response.text();
  let result = {};
  try {
    result = rawBody ? JSON.parse(rawBody) : {};
  } catch {
    throw new Error(
      `Telegram ${method}: risposta non JSON (HTTP ${response.status}): ${rawBody.slice(0, 300)}`,
    );
  }

  if (!response.ok || !result.ok) {
    throw new Error(
      `Telegram ${method}: HTTP ${response.status}: ${result.description || response.statusText || "errore senza descrizione"}`,
    );
  }
  return result.result;
}

async function ensureChat(env, chat) {
  const chatId = String(chat.id);
  await env.DB.prepare(
    `INSERT OR IGNORE INTO chats (chat_id, chat_type, active)
     VALUES (?, ?, 1)`,
  ).bind(chatId, chat.type || "unknown").run();
}

async function getChat(env, chatId) {
  return env.DB.prepare(
    `SELECT chat_id, chat_type, active,
            cards_glb, cards_jp, summons_glb, summons_jp,
            events_glb, events_jp, news_glb, news_jp
       FROM chats WHERE chat_id = ? LIMIT 1`,
  ).bind(String(chatId)).first();
}

function menuText(row) {
  const enabled = PREFS.filter((p) => Number(row?.[p.key]) === 1).length;
  return `⚙️ <b>Preferenze DokkanDB</b>\n\nSeleziona gli aggiornamenti che vuoi ricevere.\nLe modifiche vengono salvate immediatamente.\n\nAttive: <b>${enabled}/8</b>`;
}

function menuKeyboard(row) {
  const buttons = PREFS.map((pref) => {
    const isOn = Number(row?.[pref.key]) === 1;
    return {
      text: `${isOn ? "✅" : "⬜"} ${pref.label}`,
      callback_data: `set:${pref.key}:${isOn ? 0 : 1}`,
    };
  });

  return {
    inline_keyboard: [
      [buttons[0], buttons[1]],
      [buttons[2], buttons[3]],
      [buttons[4], buttons[5]],
      [buttons[6], buttons[7]],
      [
        { text: "✅ Attiva tutte", callback_data: "set:all:1" },
        { text: "⬜ Disattiva tutte", callback_data: "set:all:0" },
      ],
      [{ text: "Chiudi", callback_data: "close" }],
    ],
  };
}

async function sendUpdatesMenu(env, chatId, row) {
  await telegram(env, "sendMessage", {
    chat_id: chatId,
    text: menuText(row),
    parse_mode: "HTML",
    reply_markup: menuKeyboard(row),
    disable_web_page_preview: true,
  });
}

async function isGroupAdmin(env, chatId, userId) {
  try {
    const member = await telegram(env, "getChatMember", {
      chat_id: chatId,
      user_id: userId,
    });
    return member && ["creator", "administrator"].includes(member.status);
  } catch (error) {
    console.error("Verifica amministratore fallita:", error);
    return false;
  }
}

async function requireGroupAdmin(env, messageOrCallback) {
  const chat = messageOrCallback.chat || messageOrCallback.message?.chat;
  const from = messageOrCallback.from;
  if (!chat || !GROUP_TYPES.has(chat.type)) return true;
  return isGroupAdmin(env, chat.id, from?.id);
}

function parseCommand(text) {
  const match = String(text || "").trim().match(/^\/(start|stop|updates)(?:@[A-Za-z0-9_]+)?(?:\s|$)/i);
  return match ? match[1].toLowerCase() : "";
}

async function handleMessage(env, message) {
  const chat = message.chat;
  const command = parseCommand(message.text);
  if (!chat || !command) return;

  if (GROUP_TYPES.has(chat.type) && !(await requireGroupAdmin(env, message))) {
    if (command === "updates" || command === "stop" || command === "start") {
      await telegram(env, "sendMessage", {
        chat_id: chat.id,
        text: "⛔ Solo gli amministratori del gruppo possono modificare queste impostazioni.",
        reply_to_message_id: message.message_id,
      });
    }
    return;
  }

  if (command === "start") {
    await ensureChat(env, chat);
    await env.DB.prepare(
      "UPDATE chats SET active = 1, chat_type = ?, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
    ).bind(chat.type || "unknown", String(chat.id)).run();
    await telegram(env, "sendMessage", {
      chat_id: chat.id,
      text: "✅ Iscrizione DokkanDB attiva.\n\nUsa /updates per scegliere quali aggiornamenti ricevere.",
    });
    return;
  }

  if (command === "stop") {
    await ensureChat(env, chat);
    await env.DB.prepare(
      "UPDATE chats SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
    ).bind(String(chat.id)).run();
    await telegram(env, "sendMessage", {
      chat_id: chat.id,
      text: "⏸️ Notifiche DokkanDB disattivate. Per riattivarle usa /start.",
    });
    return;
  }

  if (command === "updates") {
    await ensureChat(env, chat);
    const row = await getChat(env, chat.id);
    await sendUpdatesMenu(env, chat.id, row);
  }
}

async function handleCallback(env, callback) {
  const message = callback.message;
  const chat = message?.chat;
  if (!message || !chat) {
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: callback.id,
      text: "Questo menu non è più disponibile.",
      show_alert: true,
    });
    return;
  }

  if (GROUP_TYPES.has(chat.type) && !(await isGroupAdmin(env, chat.id, callback.from?.id))) {
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: callback.id,
      text: "Solo gli amministratori possono modificare le preferenze del gruppo.",
      show_alert: true,
    });
    return;
  }

  await ensureChat(env, chat);
  const data = String(callback.data || "");

  if (data === "close") {
    await telegram(env, "editMessageReplyMarkup", {
      chat_id: chat.id,
      message_id: message.message_id,
      reply_markup: { inline_keyboard: [] },
    });
    await telegram(env, "answerCallbackQuery", { callback_query_id: callback.id, text: "Menu chiuso." });
    return;
  }

  const match = data.match(/^set:([a-z_]+):([01])$/);
  if (!match) {
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: callback.id,
      text: "Selezione non riconosciuta.",
      show_alert: true,
    });
    return;
  }

  const key = match[1];
  const value = Number(match[2]);
  if (key === "all") {
    const updates = PREFS.map((pref) => `${pref.key} = ?`).join(", ");
    await env.DB.prepare(
      `UPDATE chats SET ${updates}, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?`,
    ).bind(...PREFS.map(() => value), String(chat.id)).run();
  } else {
    if (!PREFS.some((pref) => pref.key === key)) {
      await telegram(env, "answerCallbackQuery", {
        callback_query_id: callback.id,
        text: "Categoria non riconosciuta.",
        show_alert: true,
      });
      return;
    }
    await env.DB.prepare(
      `UPDATE chats SET ${key} = ?, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?`,
    ).bind(value, String(chat.id)).run();
  }

  const row = await getChat(env, chat.id);
  try {
    await telegram(env, "editMessageText", {
      chat_id: chat.id,
      message_id: message.message_id,
      text: menuText(row),
      parse_mode: "HTML",
      reply_markup: menuKeyboard(row),
      disable_web_page_preview: true,
    });
  } catch (error) {
    // Telegram can report this when it retries a callback that was already applied.
    if (!String(error.message || error).includes("message is not modified")) throw error;
  }
  try {
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: callback.id,
      text: "Preferenze salvate!",
    });
  } catch (error) {
    // The UI may already have been updated if Telegram considers the callback too old.
    console.warn("Risposta callback non inviata:", error);
  }
}

async function handleMyChatMember(env, update) {
  const chat = update.chat;
  const member = update.new_chat_member || {};
  if (!chat || !["private", "group", "supergroup"].includes(chat.type)) return;

  const isMember = ACTIVE_STATUSES.has(member.status) ||
    (member.status === "restricted" && member.is_member === true);

  if (isMember) {
    await ensureChat(env, chat);
    await env.DB.prepare(
      "UPDATE chats SET active = 1, chat_type = ?, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
    ).bind(chat.type || "unknown", String(chat.id)).run();
  } else {
    await env.DB.prepare(
      "UPDATE chats SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?",
    ).bind(String(chat.id)).run();
  }
}

async function handleWebhook(env, request) {
  const secret = request.headers.get("x-telegram-bot-api-secret-token") || "";
  if (!env.TELEGRAM_WEBHOOK_SECRET || secret !== env.TELEGRAM_WEBHOOK_SECRET) {
    return new Response("Forbidden", { status: 403 });
  }

  const update = await request.json();
  try {
    if (update.message) await handleMessage(env, update.message);
    if (update.callback_query) await handleCallback(env, update.callback_query);
    if (update.my_chat_member) await handleMyChatMember(env, update.my_chat_member);
    return json({ ok: true });
  } catch (error) {
    console.error("Errore webhook Telegram:", error);
    // Non-2xx tells Telegram to retry transient failures. Preference callbacks are
    // idempotent because they SET a value rather than blindly toggling it.
    return json({ ok: false, error: "Errore interno" }, 500);
  }
}

async function importLegacyChats(env, request) {
  if (!apiAuthorized(request, env)) return json({ ok: false, error: "Unauthorized" }, 401);
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ ok: false, error: "JSON non valido" }, 400);
  }
  const chatIds = Array.isArray(body.chat_ids) ? body.chat_ids.slice(0, 1000) : [];
  const statements = [];
  for (const value of chatIds) {
    const chatId = String(value || "").trim();
    if (!/^-?\d{1,25}$/.test(chatId)) continue;
    statements.push(env.DB.prepare(
      `INSERT OR IGNORE INTO chats (chat_id, chat_type, active)
       VALUES (?, 'legacy', 1)`,
    ).bind(chatId));
  }
  if (statements.length) await env.DB.batch(statements);
  return json({ ok: true, imported_candidates: statements.length });
}

async function listPreferences(request, env) {
  if (!apiAuthorized(request, env)) return json({ ok: false, error: "Unauthorized" }, 401);
  const result = await env.DB.prepare(
    `SELECT chat_id, chat_type, active,
            cards_glb, cards_jp, summons_glb, summons_jp,
            events_glb, events_jp, news_glb, news_jp
       FROM chats WHERE active = 1 ORDER BY chat_id`,
  ).all();
  return json({ ok: true, chats: result.results || [] });
}

async function setupWebhook(request, env, url) {
  if (!apiAuthorized(request, env)) return json({ ok: false, error: "Unauthorized" }, 401);
  if (!env.TELEGRAM_WEBHOOK_SECRET) {
    return json({ ok: false, error: "TELEGRAM_WEBHOOK_SECRET non configurato" }, 500);
  }
  const webhookUrl = `${url.origin}/telegram/webhook`;
  let webhook;
  try {
    webhook = await telegram(env, "setWebhook", {
      url: webhookUrl,
      secret_token: env.TELEGRAM_WEBHOOK_SECRET,
      allowed_updates: ["message", "my_chat_member", "callback_query"],
      drop_pending_updates: false,
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    console.error("setupWebhook: setWebhook fallito:", detail);
    return json({ ok: false, step: "setWebhook", error: detail }, 502);
  }

  try {
    await telegram(env, "setMyCommands", {
      commands: [
        { command: "start", description: "Attiva le notifiche DokkanDB" },
        { command: "stop", description: "Disattiva le notifiche DokkanDB" },
        { command: "updates", description: "Scegli quali aggiornamenti ricevere" },
      ],
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    console.error("setupWebhook: setMyCommands fallito:", detail);
    return json({ ok: false, step: "setMyCommands", error: detail, webhook_set: true }, 502);
  }

  return json({ ok: true, webhook: Boolean(webhook), url: webhookUrl });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname === "/health" && request.method === "GET") {
        return json({ ok: true, service: "dokkandb-telegram" });
      }
      if (url.pathname === "/telegram/webhook" && request.method === "POST") {
        return await handleWebhook(env, request);
      }
      if (url.pathname === "/api/preferences" && request.method === "GET") {
        return await listPreferences(request, env);
      }
      if (url.pathname === "/api/import-legacy" && request.method === "POST") {
        return await importLegacyChats(env, request);
      }
      if (url.pathname === "/internal/setup-webhook" && request.method === "POST") {
        return await setupWebhook(request, env, url);
      }
      return json({ ok: false, error: "Not found" }, 404);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      const stack = error instanceof Error ? error.stack : "";
      // Log the message separately: structured Workers logs may display only the stack.
      console.error("Errore Worker (dettaglio):", detail);
      if (stack) console.error("Errore Worker (stack):", stack);
      return json({ ok: false, error: "Errore interno" }, 500);
    }
  },
};

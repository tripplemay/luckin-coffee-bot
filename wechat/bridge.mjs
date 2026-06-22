// 微信 (wx-link / 腾讯 iLink) <-> Python 点单服务 桥接。
// 收到微信消息 -> POST 到 service /message -> 把返回的 actions 发回微信。
// 注意：用【专用/小号】微信登录；这是非官方协议，封号风险自负。
import fs from "node:fs";
import readline from "node:readline";
import qrcodeTerminal from "qrcode-terminal";
import { loginWithQR, WxLinkClient } from "wx-link";

const SERVICE_URL = process.env.SERVICE_URL || "http://127.0.0.1:8100";
const BRIDGE_SECRET = process.env.BRIDGE_SECRET || "";
const SESSION_FILE = new URL("./.wxsession.json", import.meta.url);
const BOT_AGENT = "coffee-bot/0.1";

function ask(q) {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((r) => rl.question(q, (a) => { rl.close(); r(a.trim()); }));
}

function loadSession() {
  try { return JSON.parse(fs.readFileSync(SESSION_FILE, "utf8")); } catch { return null; }
}
function saveSession(obj) {
  fs.writeFileSync(SESSION_FILE, JSON.stringify(obj, null, 2), { mode: 0o600 });
}

function inboundText(msg) {
  return (msg.item_list || []).map((it) => it?.text_item?.text).filter(Boolean).join("\n").trim();
}

async function callService(userKey, text, msgId) {
  const headers = { "content-type": "application/json" };
  if (BRIDGE_SECRET) headers["x-bridge-secret"] = BRIDGE_SECRET;
  const r = await fetch(SERVICE_URL + "/message", {
    method: "POST",
    headers,
    body: JSON.stringify({ user_key: userKey, text, msg_id: msgId }),
  });
  if (!r.ok) throw new Error("service HTTP " + r.status);
  return (await r.json()).actions || [];
}

async function getClient() {
  const saved = loadSession();
  if (saved?.botToken && saved?.userId) {
    console.log("复用已保存的微信会话。userId:", saved.userId);
    return {
      client: WxLinkClient.fromAccount({ baseUrl: saved.baseUrl, token: saved.botToken }, { botAgent: BOT_AGENT }),
      cursor: saved.cursor || "",
      selfId: saved.userId,
    };
  }
  if (saved?.botToken && !saved?.userId) {
    console.warn("⚠️ 旧会话缺少 userId（无法可靠过滤自身消息），将重新登录。");
  }
  const login = await loginWithQR({
    onQRCode: (url) => {
      console.log("\n用【专用/小号】微信扫码登录 👇\n");
      qrcodeTerminal.generate(url, { small: true });
      console.log("\n(二维码内容: " + url + ")\n");
    },
    onStatusChange: (s) => console.log("登录状态:", s),
    onVerifyCode: async ({ retry }) =>
      ask(retry ? "配对码不匹配，重新输入手机微信显示的数字: " : "请输入手机微信显示的配对数字: "),
  });
  saveSession({ ...login, cursor: "" });
  console.log("✅ 登录成功，会话已保存。userId:", login.userId);
  return { client: new WxLinkClient({ baseUrl: login.baseUrl, token: login.botToken, botAgent: BOT_AGENT }), cursor: "", selfId: login.userId };
}

async function main() {
  const { client, cursor: startCursor, selfId } = await getClient();
  let cursor = startCursor;
  console.log("开始轮询微信消息…（Ctrl+C 退出）");
  for (;;) {
    let updates;
    try {
      updates = await client.poll(cursor);
    } catch (e) {
      console.error("poll 出错:", e.message);
      await new Promise((r) => setTimeout(r, 3000));
      continue;
    }
    const nextCursor = updates.nextCursor || cursor;

    for (const msg of updates.msgs || []) {
      const uid = msg.from_user_id;
      if (!uid) continue;
      if (selfId && uid === selfId) continue; // 跳过自己发的消息，避免回声循环
      const ctx = msg.context_token;
      const text = inboundText(msg);
      if (!text) {
        const hasMedia = (msg.item_list || []).some((it) => it && (it.image_item || it.voice_item || it.file_item || it.video_item));
        if (hasMedia) { try { await client.sendText({ toUserId: uid, text: "目前只支持文字哦～", contextToken: ctx }); } catch {} }
        continue;
      }
      const msgId = String(msg.message_id ?? msg.seq ?? "");
      console.log(`[in] ${uid}: ${text}`);

      let actions;
      try {
        actions = await callService(uid, text, msgId);
      } catch (e) {
        console.error("调用服务失败:", e.message);
        try { await client.sendText({ toUserId: uid, text: "服务暂时不可用，请稍后再试。", contextToken: ctx }); } catch {}
        continue;
      }
      for (const a of actions) {
        try {
          if (a.type === "text") {
            await client.sendTextChunked(uid, a.text, ctx);
          } else if (a.type === "image") {
            await client.sendMediaFromBuffer({
              toUserId: uid,
              buffer: Buffer.from(a.b64, "base64"),
              fileName: "pay.png",
              contentType: "image/png",
              text: a.caption || "",
              contextToken: ctx,
            });
          }
        } catch (e) {
          console.error("发送失败:", e.message);
        }
      }
    }

    // 处理完整批后再推进/持久化 cursor（崩溃可重投，配合 service 端 msg_id 幂等去重）
    cursor = nextCursor;
    const s = loadSession();
    if (s) { s.cursor = cursor; saveSession(s); }
  }
}

main().catch((e) => { console.error("fatal:", e); process.exit(1); });

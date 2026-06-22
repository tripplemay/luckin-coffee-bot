// 微信 (wx-link / 腾讯 iLink) <-> Python 点单服务 桥接。
// 收到微信消息 -> POST 到 service /message -> 把返回的 actions 发回微信。
// 注意：用【专用/小号】微信登录；这是非官方协议，封号风险自负。
import fs from "node:fs";
import readline from "node:readline";
import qrcodeTerminal from "qrcode-terminal";
import { decode as silkDecode, isSilk, isWav } from "silk-wasm";
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

// PCM(s16le) → WAV：给 SILK 解码出的裸 PCM 套个 44 字节 WAV 头，便于服务端 ffmpeg/ASR 处理
function pcmToWav(pcm, sampleRate, channels = 1, bits = 16) {
  const blockAlign = (channels * bits) / 8;
  const h = Buffer.alloc(44);
  h.write("RIFF", 0); h.writeUInt32LE(36 + pcm.length, 4); h.write("WAVE", 8);
  h.write("fmt ", 12); h.writeUInt32LE(16, 16); h.writeUInt16LE(1, 20);
  h.writeUInt16LE(channels, 22); h.writeUInt32LE(sampleRate, 24);
  h.writeUInt32LE(sampleRate * blockAlign, 28); h.writeUInt16LE(blockAlign, 32);
  h.writeUInt16LE(bits, 34); h.write("data", 36); h.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([h, pcm]);
}

// 微信语音是 SILK v3（ffmpeg 不认）→ silk-wasm 解成 PCM → 包成 WAV
async function silkToWav(silk, sampleRate) {
  const { data } = await silkDecode(silk, sampleRate);
  return pcmToWav(Buffer.from(data), sampleRate);
}

// 下载到的语音字节 → 服务端能吃的格式：SILK 解成 WAV；已是 WAV 原样；其它(amr/mp3)交服务端 ffmpeg 兜底
async function audioToWav(buf, sampleRate) {
  if (isWav(buf)) return buf;
  if (isSilk(buf)) return silkToWav(buf, sampleRate);
  if (buf.length > 10 && isSilk(buf.subarray(1))) return silkToWav(buf.subarray(1), sampleRate); // 微信前导字节
  return buf; // ffmpeg 兜底
}

async function callVoice(client, uid, msg, v, msgId) {
  const body = { user_key: uid, msg_id: msgId };
  if (v.text && v.text.trim()) {
    body.text = v.text.trim(); // 微信已自带转写 → 省一次 ASR
  } else {
    const item = (msg.item_list || []).find((it) => it && it.voice_item);
    const dl = await client.downloadInboundMedia(item);
    if (!dl || !dl.buffer) return [{ type: "text", text: "语音下载失败，打字告诉我吧。" }];
    const wav = await audioToWav(dl.buffer, v.sample_rate || 24000);
    body.audio_b64 = wav.toString("base64");
  }
  const headers = { "content-type": "application/json" };
  if (BRIDGE_SECRET) headers["x-bridge-secret"] = BRIDGE_SECRET;
  const r = await fetch(SERVICE_URL + "/voice", { method: "POST", headers, body: JSON.stringify(body) });
  if (!r.ok) throw new Error("voice HTTP " + r.status);
  return (await r.json()).actions || [];
}

async function sendActions(client, uid, ctx, actions) {
  for (const a of actions) {
    try {
      if (a.type === "text") {
        await client.sendTextChunked(uid, a.text, ctx);
      } else if (a.type === "image") {
        await client.sendMediaFromBuffer({
          toUserId: uid, buffer: Buffer.from(a.b64, "base64"), fileName: "pay.png",
          contentType: "image/png", text: a.caption || "", contextToken: ctx,
        });
      }
    } catch (e) {
      console.error("发送失败:", e.message);
    }
  }
}

async function getClient() {
  const saved = loadSession();
  if (saved?.botToken) {
    console.log("复用已保存的微信会话。userId:", saved.userId, "botId:", saved.accountId);
    return {
      client: WxLinkClient.fromAccount({ baseUrl: saved.baseUrl, token: saved.botToken }, { botAgent: BOT_AGENT }),
      cursor: saved.cursor || "",
      selfId: saved.accountId, // 防回声：过滤机器人自身(@im.bot)，绝不能用 userId(小号本人=合法用户)
    };
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
  console.log("✅ 登录成功，会话已保存。userId:", login.userId, "botId:", login.accountId);
  return { client: new WxLinkClient({ baseUrl: login.baseUrl, token: login.botToken, botAgent: BOT_AGENT }), cursor: "", selfId: login.accountId };
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
      const voiceItem = (msg.item_list || []).find((it) => it && it.voice_item)?.voice_item;
      const msgId = String(msg.message_id ?? msg.seq ?? "");

      let actions;
      try {
        if (text) {
          console.log(`[in] ${uid}: ${text}`);
          actions = await callService(uid, text, msgId);
        } else if (voiceItem) {
          console.log(`[in voice] ${uid}: sr=${voiceItem.sample_rate} hasText=${!!(voiceItem.text && voiceItem.text.trim())}`);
          actions = await callVoice(client, uid, msg, voiceItem, msgId);
        } else {
          const hasMedia = (msg.item_list || []).some((it) => it && (it.image_item || it.file_item || it.video_item));
          if (hasMedia) { try { await client.sendText({ toUserId: uid, text: "目前只支持文字和语音哦～", contextToken: ctx }); } catch {} }
          continue;
        }
      } catch (e) {
        console.error("处理失败:", e.message);
        try { await client.sendText({ toUserId: uid, text: "服务暂时不可用，请稍后再试。", contextToken: ctx }); } catch {}
        continue;
      }
      await sendActions(client, uid, ctx, actions);
    }

    // 处理完整批后再推进/持久化 cursor（崩溃可重投，配合 service 端 msg_id 幂等去重）
    cursor = nextCursor;
    const s = loadSession();
    if (s) { s.cursor = cursor; saveSession(s); }
  }
}

main().catch((e) => { console.error("fatal:", e); process.exit(1); });

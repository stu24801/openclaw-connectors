const express = require('express');
const line = require('@line/bot-sdk');
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

/**
 * LINE Bridge v5.4 (Support Messaging Tool)
 * - 修正 /internal/send 支援 `to` 與 `target` 兩種欄位名稱
 * - 增加額度不足時的明確錯誤提示
 */

const CONFIG_DIR = process.env.LINE_BRIDGE_CONFIG_DIR || process.env.HOME || '/tmp';
const CLAWDBOT_BIN = process.env.CLAWDBOT_BIN || 'clawdbot';
const CUSTOM_PATH = process.env.LINE_BRIDGE_PATH || process.env.PATH || '/usr/local/bin:/usr/bin:/bin';

const tokenPath = path.join(CONFIG_DIR, '.line_token');
const secretPath = path.join(CONFIG_DIR, '.line_secret');
const allowedSourcesPath = path.join(CONFIG_DIR, '.line_allowed_sources');

try {
  if (!fs.existsSync(tokenPath) || !fs.existsSync(secretPath) || !fs.existsSync(allowedSourcesPath)) {
    console.error(`[Init Error] Missing config files in ${CONFIG_DIR}`);
    process.exit(1);
  }

  const config = {
    channelAccessToken: fs.readFileSync(tokenPath, 'utf8').trim(),
    channelSecret: fs.readFileSync(secretPath, 'utf8').trim()
  };

  const client = new line.Client(config);
  const app = express();

  // 1. Webhook 端點 (接收訊息) - 必須在 express.json() 之前，因為 line.middleware 需要 raw body
  app.post('/line/webhook', line.middleware(config), async (req, res) => {
    res.status(200).send('OK');
    for (const event of req.body.events) {
      if (event.type === 'message' && event.message.type === 'text') {
        const sourceId = event.source.groupId || event.source.roomId || event.source.userId;
        const senderId = event.source.userId;
        const allowedSources = fs.readFileSync(allowedSourcesPath, 'utf8').split('\n').map(s => s.trim()).filter(s => s.length > 0);

        if (!allowedSources.includes(sourceId)) continue;

        try {
          let senderName = "朋友";
          try {
            const p = event.source.groupId
              ? await client.getGroupMemberProfile(event.source.groupId, senderId)
              : await client.getProfile(senderId);
            senderName = p.displayName;
          } catch(e) {}

          const formattedMsg = `[SenderName: ${senderName}] [SenderID: ${senderId}] [SourceID: ${sourceId}] ${event.message.text}`;
          const cmd = `${CLAWDBOT_BIN} agent --session-id "main" --message "${formattedMsg.replace(/"/g, '')}" --json`;
          const result = JSON.parse(execSync(cmd, { env: { ...process.env, PATH: CUSTOM_PATH } }).toString());

          let agentReply = '';
          if (result && result.message) agentReply = result.message;
          else if (result && result.result && result.result.payloads) {
            const p = result.result.payloads.find(x => x.type === 'text' || x.text);
            agentReply = p ? (p.text || p.body) : '';
          }

          if (agentReply) {
            try {
              let quotaInfo = "";
              try {
                const [quota, consumption] = await Promise.all([
                  client.getMessageQuota().catch(() => null),
                  client.getMessageQuotaConsumption().catch(() => null)
                ]);
                if (quota && consumption) {
                  const limit = quota.type === 'limited' ? quota.value : '無限制';
                  const used = consumption.totalUsage;
                  const remaining = quota.type === 'limited' ? (quota.value - used) : 'Inf';
                  console.log(`[Quota] Used: ${used}, Limit: ${limit}, Remaining: ${remaining}`);
                  if (quota.type === 'limited' && remaining < 50) {
                    quotaInfo = `\n⚠️ LINE額度剩餘: ${remaining}/${limit}`;
                  }
                }
              } catch (qErr) {
                console.error('[Quota Check Error]', qErr.message);
              }

              await client.pushMessage(sourceId, { type: 'text', text: agentReply + quotaInfo });
            } catch (sendErr) {
              console.error('[Send Error]', sendErr.statusCode, sendErr.statusMessage);
              if (sendErr.statusCode === 429) {
                console.error('⚠️ LINE Monthly Limit Exceeded. Message dropped.');
              }
            }
          }
        } catch (err) { console.error('[Process Error]', err); }
      }
    }
  });

  // 2. 內部發送端點 (讓大腦可以透過 curl 發送訊息或圖片)
  //    支援 `to` 或 `target` 作為目標 ID 欄位
  //    支援 `message` 或 `messages` 作為訊息欄位
  app.post('/internal/send', express.json(), async (req, res) => {
    const targetId = req.body.to || req.body.target;
    const { imageUrl, previewUrl } = req.body;

    // 支援兩種格式：{ message: "..." } 或 { messages: [{type:"text", text:"..."}] }
    let textMessage = req.body.message;
    if (!textMessage && Array.isArray(req.body.messages)) {
      const first = req.body.messages.find(m => m.type === 'text');
      if (first) textMessage = first.text;
    }

    if (!targetId) {
      return res.status(400).json({ error: 'Missing target id (use "to" or "target" field)' });
    }

    try {
      if (imageUrl) {
        await client.pushMessage(targetId, {
          type: 'image',
          originalContentUrl: imageUrl,
          previewImageUrl: previewUrl || imageUrl
        });
      }
      if (textMessage) {
        await client.pushMessage(targetId, { type: 'text', text: textMessage });
      }
      res.json({ ok: true });
    } catch (e) {
      console.error('[Internal Send Error]', e.statusCode, e.message);
      if (e.statusCode === 429) {
        return res.status(429).json({ error: 'LINE monthly limit exceeded', code: 429 });
      }
      res.status(500).json({ error: e.message, code: e.statusCode });
    }
  });

  const PORT = process.env.PORT || 8081;
  app.listen(PORT, '0.0.0.0', () => {
    console.log(`LINE Bridge v5.4 (Internal API) ONLINE on port ${PORT}`);
  });
} catch (err) { console.error(err); }

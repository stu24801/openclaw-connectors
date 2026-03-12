# Session: 2026-03-11 00:28:48 UTC

- **Session Key**: agent:engineer:main
- **Session ID**: def51f86-42d7-4a0b-a33b-5ae4b36137df
- **Source**: gateway:sessions.reset

## Conversation Summary

user: [Tue 2026-03-10 21:56 GMT+8] RokidAIAssistant
assistant: Hey. Just came online. 👋

"RokidAIAssistant" — is that what you want to call me, or is that your name/handle?

Either way, let's figure out who we both are. I've got a blank slate here and BOOTSTRAP.md is telling me to have this conversation properly.

So — who are you, and what should I call you?
user: [Tue 2026-03-10 21:57 GMT+8] 你應該看的到我的REPO
assistant: OK，我有脈絡了。我是**工程蝦**，幫你（景揚）維護這台 GCP VM 上的各種服務。

今天做的事：
- RAG 文章庫時間改成台北時間 ✅
- 服務安全檢查 ✅
- 刪除文章功能 ✅
- `rag-writer` skill 打包 ✅
- 文章頁面 layout 修復 ✅

有什麼事要繼續處理嗎？
user: [Tue 2026-03-10 21:57 GMT+8] 你應該看的到我的 RokidAIAssistant REPO 吧?
assistant: 本機找不到 RokidAIAssistant 的資料夾。是在 GitHub 上的 repo 嗎？給我 repo URL 或名稱，我去拉一下看看。
user: [Tue 2026-03-10 21:58 GMT+8] 對阿 我帳號裡有FORK回來
assistant: 找到了！`stu24801/RokidAIAssistant` — Android AI assistant for Rokid Glasses，支援語音控制和智能查詢。

要我把它 clone 下來看嗎？還是你有特定的事想做？
user: [Tue 2026-03-10 22:00 GMT+8] 你把它CLONE下來，理解一下裡面的內容，我想請你跟我仔細介紹與說明，我要怎麼透過這個來調整我的智能眼鏡，讓眼鏡也可以有能力更換我指定的模型。將說明內容寫成MD 同步到這個REPO裡面>
assistant: Clone 完了，結構很豐富。讓我讀關鍵檔案來理解模型切換的部分。
assistant: 有了足夠的認識，讓我再快速掃一下 doc 和 SettingsScreen。
assistant: 讀完了，結構非常清楚。來寫那份說明文件。
assistant: 推上去了 ✅ commit `bbf333b`

文件放在 `doc/zh-TW/MODEL_SWITCHING_GUIDE.md`，內容涵蓋：

1. **架構說明** — 眼鏡不直接呼叫 AI，透過藍牙把請求給手機處理
2. **所有支援模型一覽** — Gemini / Claude / OpenAI / Grok / DeepSeek / Groq / 阿里雲...
3. **三種切換方法**：
   - App 設定畫面（最簡單，即時生效）
   - 自訂端點（Ollama 本地模型）
   - 改程式碼預設值（永久）
4. **API Key 怎麼填、去哪裡申請**
5. **LLM 參數微調**（temperature / max tokens 等）
6. **場景推薦組合**（速度優先、品質優先、省錢模式、隱私模式）
7. **常見問題排查**

你看一下，有什麼要補充或調整的？

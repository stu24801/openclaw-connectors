# GitHub Rate Limit Connector for Microsoft Copilot Studio

這是一個用於 Microsoft Copilot Studio (前身為 Power Virtual Agents) 的 **自定義連接器 (Custom Connector)** 定義，允許您的 Copilot 查詢 GitHub API 的速率限制狀態。

## 如何匯入到 Copilot Studio

1.  **下載 OpenAPI 定義檔**：
    - 下載本目錄中的 [`openapi.yaml`](./openapi.yaml) 檔案。

2.  **登入 Copilot Studio**：
    - 前往 [Microsoft Copilot Studio](https://web.powerva.microsoft.com/)。

3.  **建立自定義連接器**：
    - 在左側選單中，選擇 **"Connectors"** (連接器)。
    - 點擊 **"New custom connector"** (新增自定義連接器) -> **"Import an OpenAPI file"** (匯入 OpenAPI 檔案)。
    - 輸入連接器名稱 (例如：`GitHub Rate Limit Checker`)。
    - 選擇剛才下載的 `openapi.yaml` 檔案。
    - 點擊 **"Continue"** (繼續)。

4.  **設定安全性 (Security)**：
    - **Authentication type** (驗證類型)：選擇 `API Key` 或 `No authentication`。
      - 如果您想查詢**公開 IP** 的限制 (每小時 60 次)，選擇 `No authentication`。
      - 如果您想查詢**特定帳號** 的限制 (每小時 5000 次)，選擇 `API Key`。
        - **Parameter label**: `Token`
        - **Parameter name**: `Authorization`
        - **Parameter location**: `Header`
    - 點擊 **"Create connector"** (建立連接器)。

5.  **在 Copilot 中使用**：
    - 在您的 Copilot 話題 (Topic) 中，新增一個 **"Call an action"** (呼叫動作) 節點。
    - 搜尋並選擇 `GitHub Rate Limit Checker`。
    - 選擇 `GetRateLimit` 動作。
    - 您可以使用輸出的 `resources.core.remaining` (剩餘次數) 和 `resources.core.reset` (重置時間) 來回應使用者。

## 功能

- **GetRateLimit**: 取得目前的 API 速率限制狀態，包括 Core API、Search API 等。

## 範例回應

```json
{
  "resources": {
    "core": {
      "limit": 5000,
      "remaining": 4999,
      "reset": 1372700873,
      "used": 1
    },
    ...
  }
}
```

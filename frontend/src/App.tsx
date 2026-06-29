import { useEffect, useState } from "react";
import AsrPage from "./pages/AsrPage";
import TtsPage from "./pages/TtsPage";
import ChatPage from "./pages/ChatPage";
import { checkReady } from "./api/client";

export default function App() {
  const [tab, setTab] = useState<"asr" | "tts" | "chat">("asr");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      const r = await checkReady();
      if (alive) setReady(r);
    };
    poll();
    const id = setInterval(poll, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  return (
    <div className="app">
      <h1>
        语音大模型 · ASR / TTS
        <span className="status">
          <span className={`dot ${ready ? "ok" : "bad"}`} />
          {ready ? "服务就绪" : "加载中"}
        </span>
      </h1>
      <p className="subtitle">FunASR 语音识别 + CosyVoice 语音合成 · 文件式 / 流式</p>

      <div className="tabs">
        <button className={`tab ${tab === "asr" ? "active" : ""}`} onClick={() => setTab("asr")}>
          语音识别 (ASR)
        </button>
        <button className={`tab ${tab === "tts" ? "active" : ""}`} onClick={() => setTab("tts")}>
          语音合成 (TTS)
        </button>
        <button className={`tab ${tab === "chat" ? "active" : ""}`} onClick={() => setTab("chat")}>
          语音对话 (Chat)
        </button>
      </div>

      {tab === "asr" && <AsrPage />}
      {tab === "tts" && <TtsPage />}
      {tab === "chat" && <ChatPage />}
    </div>
  );
}

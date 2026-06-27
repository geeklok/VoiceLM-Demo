import { useRef, useState } from "react";
import { asrFile, openAsrStream } from "../api/client";
import { MicRecorder, TARGET_SR } from "../audio/recorder";

export default function AsrPage() {
  const [language, setLanguage] = useState("auto");
  const [hotwords, setHotwords] = useState("");
  const [text, setText] = useState("");
  const [meta, setMeta] = useState("");
  const [busy, setBusy] = useState(false);
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState("");

  const wsRef = useRef<WebSocket | null>(null);
  const recRef = useRef<MicRecorder | null>(null);

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setError("");
    setText("");
    setMeta("");
    try {
      const res = await asrFile(file, language, hotwords);
      setText(res.text);
      setMeta(
        `模型 ${res.model} · 音频 ${res.audio_duration_ms}ms · 处理 ${res.process_ms}ms · RTF ${res.rtf}`
      );
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
      e.target.value = "";
    }
  }

  async function toggleRecord() {
    if (recording) {
      recRef.current?.stop();
      wsRef.current?.send(JSON.stringify({ type: "end" }));
      setRecording(false);
      return;
    }
    setError("");
    setText("");
    setMeta("实时转写中...");
    const ws = openAsrStream(
      TARGET_SR,
      language,
      (t, isFinal) => {
        setText(t);
        if (isFinal) setMeta("转写完成");
      },
      (msg) => setError(msg)
    );
    wsRef.current = ws;

    const rec = new MicRecorder((pcm) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(pcm.buffer);
    });
    recRef.current = rec;
    try {
      await rec.start();
      setRecording(true);
    } catch (err) {
      setError("无法访问麦克风: " + String(err));
      ws.close();
    }
  }

  return (
    <div className="panel">
      <div className="row">
        <div>
          <label>语言</label>
          <select value={language} onChange={(e) => setLanguage(e.target.value)}>
            <option value="auto">自动</option>
            <option value="zh">中文</option>
            <option value="en">English</option>
          </select>
        </div>
        <div>
          <label>热词 (逗号分隔, 可选)</label>
          <input type="text" value={hotwords} onChange={(e) => setHotwords(e.target.value)} />
        </div>
      </div>

      <label>上传音频文件 (任意格式/采样率)</label>
      <input type="file" accept="audio/*,video/*" onChange={onUpload} disabled={busy || recording} />

      <div style={{ marginTop: 16 }}>
        <button className={`ghost rec ${recording ? "active" : ""}`} onClick={toggleRecord}>
          {recording ? "■ 停止录音" : "● 实时录音转写"}
        </button>
      </div>

      {error && <div className="result" style={{ color: "#f87171" }}>{error}</div>}
      <div className="result">{text || (busy ? "识别中..." : "识别结果将显示在这里")}</div>
      {meta && <div className="meta">{meta}</div>}
    </div>
  );
}

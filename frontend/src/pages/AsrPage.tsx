import { useEffect, useRef, useState } from "react";
import { asrFile, fetchModels, openAsrStream, ModelInfo } from "../api/client";
import { MicRecorder, TARGET_SR } from "../audio/recorder";
import { QosBadge } from "../components/QosBadge";

interface AsrQos {
  node?: string;
  model?: string;
  processMs?: number | null;
  audioMs?: number | null;
  rtf?: number | null;
  degraded?: boolean;
  mode?: string;
}

export default function AsrPage() {
  const [language, setLanguage] = useState("auto");
  const [hotwords, setHotwords] = useState("");
  const [committed, setCommitted] = useState<Record<number, string>>({});
  const [partial, setPartial] = useState<{ id: number; text: string } | null>(null);
  const [meta, setMeta] = useState("");
  const [qos, setQos] = useState<AsrQos | null>(null);
  const [busy, setBusy] = useState(false);
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState("");
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [model, setModel] = useState("");

  const wsRef = useRef<WebSocket | null>(null);
  const recRef = useRef<MicRecorder | null>(null);

  const committedText = Object.keys(committed)
    .map(Number)
    .sort((a, b) => a - b)
    .map((k) => committed[k])
    .join("");
  const hasContent = committedText.length > 0 || (partial?.text?.length ?? 0) > 0;

  // 当前选中模型是否支持热词。未知模型 (列表未加载) 时默认允许编辑, 交由后端忽略。
  const selectedModel = models.find((m) => m.name === model);
  const hotwordsSupported = selectedModel ? !!selectedModel.supports_hotwords : true;

  useEffect(() => {
    fetchModels()
      .then((res) => {
        setModels(res.asr);
        const def = res.asr.find((m) => m.default) ?? res.asr[0];
        if (def) setModel(def.name);
      })
      .catch(() => {});
  }, []);

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setError("");
    setCommitted({});
    setPartial(null);
    setMeta("");
    setQos(null);
    try {
      const res = await asrFile(
        file,
        language,
        hotwordsSupported ? hotwords : "",
        model || undefined
      );
      setCommitted({ 0: res.text });
      setMeta(`音频 ${res.audio_duration_ms}ms`);
      setQos({
        node: res.node,
        model: res.model,
        processMs: res.process_ms,
        audioMs: res.audio_duration_ms,
        rtf: res.rtf,
        degraded: res.degraded,
        mode: "file",
      });
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
      // 仅发 end 收尾, 不主动 close: 非流式引擎 (sensevoice/paraformer) 在录音中只吐
      // 时长占位符, 要等收到 end 把整段音频跑一次离线识别后才下发 final。抢先 close
      // 会丢掉这条 final, 占位符停在 "... (Xs)" 不更新。等服务端发完 final 自行关闭,
      // 由 ws.onclose 复位 UI。
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "end" }));
      } else {
        wsRef.current?.close();
      }
      setRecording(false);
      return;
    }
    setError("");
    setCommitted({});
    setPartial(null);
    setMeta("实时转写中...");
    setQos(null);
    const ws = openAsrStream(
      TARGET_SR,
      language,
      (t, isFinal, segmentId, node) => {
        if (isFinal) {
          setCommitted((prev) => ({ ...prev, [segmentId]: t }));
          setPartial((prev) => (prev?.id === segmentId ? null : prev));
        } else {
          setPartial({ id: segmentId, text: t });
        }
        if (node) setQos({ node, mode: "stream" });
      },
      (msg) => setError(msg),
      model || undefined,
      hotwordsSupported ? hotwords || undefined : undefined
    );
    wsRef.current = ws;
    // 服务端发完 final 后会主动 close (见 routes_asr)。此处统一收尾: 清掉 "实时转写中..."
    // 提示并复位录音态 (兼顾正常停止与连接异常断开)。仅当它仍是当前 socket 时才动 UI,
    // 避免「快速停止→再开始」时旧连接的迟到 close 误伤新一轮录音。
    ws.onclose = () => {
      if (wsRef.current !== ws) return;
      wsRef.current = null;
      setMeta((m) => (m === "实时转写中..." ? "" : m));
      setRecording(false);
    };

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
        {models.length > 1 && (
          <div>
            <label>ASR 模型</label>
            <select value={model} onChange={(e) => setModel(e.target.value)} disabled={recording}>
              {models.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name}
                  {m.default ? " (默认)" : ""}
                </option>
              ))}
            </select>
          </div>
        )}
        <div>
          <label>
            热词 (逗号分隔, 可选)
            {selectedModel && !hotwordsSupported && (
              <span style={{ color: "#9ca3af", fontWeight: "normal" }}>
                {" "}
                — 当前模型不支持
              </span>
            )}
          </label>
          <input
            type="text"
            value={hotwordsSupported ? hotwords : ""}
            onChange={(e) => setHotwords(e.target.value)}
            disabled={!hotwordsSupported}
            placeholder={hotwordsSupported ? "" : "该模型不支持热词, 请选用 funasr-seaco"}
            title={
              hotwordsSupported
                ? ""
                : "该模型不支持热词偏置; 需真热词请选择支持的模型 (如 funasr-seaco)"
            }
          />
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
      <div className="result">
        {hasContent ? (
          <>
            <span>{committedText}</span>
            {partial && <span className="asr-partial">{partial.text}</span>}
          </>
        ) : busy ? (
          "识别中..."
        ) : (
          "识别结果将显示在这里"
        )}
      </div>
      {meta && <div className="meta">{meta}</div>}
      {qos && (
        <QosBadge
          node={qos.node}
          model={qos.model}
          mode={qos.mode}
          processMs={qos.processMs}
          audioMs={qos.audioMs}
          rtf={qos.rtf}
          degraded={qos.degraded}
        />
      )}
    </div>
  );
}

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

type StreamState = "idle" | "connecting" | "recording" | "finalizing";

const LANGUAGE_LABELS: Record<string, string> = {
  auto: "自动",
  zh: "中文",
  en: "English",
  yue: "粤语",
  ja: "日本語",
  ko: "한국어",
};

export default function AsrPage() {
  const [language, setLanguage] = useState("auto");
  const [hotwords, setHotwords] = useState("");
  const [committed, setCommitted] = useState<Record<number, string>>({});
  const [partial, setPartial] = useState<{ id: number; text: string } | null>(null);
  const [meta, setMeta] = useState("");
  const [qos, setQos] = useState<AsrQos | null>(null);
  const [busy, setBusy] = useState(false);
  const [streamState, setStreamState] = useState<StreamState>("idle");
  const [error, setError] = useState("");
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [model, setModel] = useState("");

  const wsRef = useRef<WebSocket | null>(null);
  const recRef = useRef<MicRecorder | null>(null);
  const recording = streamState === "recording";
  const streamActive = streamState !== "idle";

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
      .catch((err) => setError("加载 ASR 模型失败: " + String(err)));
  }, []);

  useEffect(
    () => () => {
      recRef.current?.stop();
      wsRef.current?.close();
    },
    []
  );

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
    if (streamState === "connecting") {
      wsRef.current?.close();
      setStreamState("idle");
      setMeta("");
      return;
    }
    if (recording) {
      recRef.current?.stop();
      recRef.current = null;
      // 仅发 end 收尾, 不主动 close: 非流式引擎 (sensevoice/paraformer) 在录音中只吐
      // 时长占位符, 要等收到 end 把整段音频跑一次离线识别后才下发 final。抢先 close
      // 会丢掉这条 final, 占位符停在 "... (Xs)" 不更新。等服务端发完 final 自行关闭,
      // 由 ws.onclose 复位 UI。
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "end" }));
      } else {
        wsRef.current?.close();
      }
      setStreamState("finalizing");
      setMeta("正在生成最终结果...");
      return;
    }
    if (streamState === "finalizing") return;
    setError("");
    setCommitted({});
    setPartial(null);
    setMeta("正在连接 ASR 服务...");
    setQos(null);
    setStreamState("connecting");
    let ws: WebSocket;
    ws = openAsrStream(
      TARGET_SR,
      language,
      {
        onReady: (node, readyModel) => {
          if (wsRef.current !== ws) return;
          if (node || readyModel) {
            setQos({ node, model: readyModel, mode: "stream" });
          }
          startMic(ws);
        },
        onPartial: (t, isFinal, segmentId, node) => {
          if (isFinal) {
            setCommitted((prev) => ({ ...prev, [segmentId]: t }));
            setPartial((prev) => (prev?.id === segmentId ? null : prev));
          } else {
            setPartial({ id: segmentId, text: t });
          }
          if (node) setQos((prev) => ({ ...prev, node, mode: "stream" }));
        },
        onError: (msg, _code, retryAfter) => {
          setError(
            retryAfter ? `${msg}，可在 ${retryAfter} 秒后重试` : msg
          );
        },
        onClose: (expected) => {
          if (wsRef.current !== ws) return;
          recRef.current?.stop();
          recRef.current = null;
          wsRef.current = null;
          setStreamState((prev) => {
            if (!expected && prev !== "idle" && prev !== "finalizing") {
              setError((cur) => cur || "ASR 连接已断开，请重试");
            }
            return "idle";
          });
          setMeta("");
        },
      },
      model || undefined,
      hotwordsSupported ? hotwords || undefined : undefined
    );
    wsRef.current = ws;
  }

  async function startMic(ws: WebSocket) {
    const rec = new MicRecorder((pcm) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(pcm.buffer);
    }, { preRollMs: 200, captureProfile: "noise_reduction" });
    recRef.current = rec;
    try {
      await rec.start();
      if (wsRef.current !== ws || ws.readyState !== WebSocket.OPEN) {
        rec.stop();
        if (recRef.current === rec) recRef.current = null;
        return;
      }
      setStreamState("recording");
      setMeta("实时转写中...");
    } catch (err) {
      if (wsRef.current !== ws) return;
      setError("无法访问麦克风: " + String(err));
      ws.close();
    }
  }

  const languageOptions =
    selectedModel?.languages?.length
      ? Array.from(new Set(["auto", ...selectedModel.languages]))
      : ["auto", "zh", "en"];

  function selectModel(name: string) {
    setModel(name);
    const selected = models.find((item) => item.name === name);
    if (
      selected &&
      language !== "auto" &&
      !selected.languages.includes(language)
    ) {
      setLanguage("auto");
    }
  }

  return (
    <div className="panel">
      <div className="row">
        <div>
          <label>语言</label>
          <select value={language} onChange={(e) => setLanguage(e.target.value)}>
            {languageOptions.map((item) => (
              <option key={item} value={item}>
                {LANGUAGE_LABELS[item] || item}
              </option>
            ))}
          </select>
        </div>
        {models.length > 1 && (
          <div>
            <label>ASR 模型</label>
            <select value={model} onChange={(e) => selectModel(e.target.value)} disabled={streamActive}>
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
      <input type="file" accept="audio/*,video/*" onChange={onUpload} disabled={busy || streamActive} />

      <div style={{ marginTop: 16 }}>
        <button
          className={`ghost rec ${recording ? "active" : ""}`}
          onClick={toggleRecord}
          disabled={streamState === "finalizing"}
        >
          {streamState === "connecting"
            ? "取消连接"
            : streamState === "recording"
              ? "■ 停止录音"
              : streamState === "finalizing"
                ? "正在定稿..."
                : "● 实时录音转写"}
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

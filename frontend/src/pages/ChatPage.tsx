import { useEffect, useRef, useState } from "react";
import {
  ChatMode,
  ChatModelInfo,
  ChatQos,
  fetchChatModels,
  openChatStream,
} from "../api/client";
import { MicRecorder, TARGET_SR } from "../audio/recorder";
import { StreamingPcmPlayer } from "../audio/player";

interface ChatMessage {
  role: "user" | "assistant";
  text: string;
  qos?: ChatQos;
  node?: string;
}

type TurnState = "" | "listening" | "thinking" | "responding";

const STATE_LABEL: Record<TurnState, string> = {
  "": "未连接",
  listening: "聆听中",
  thinking: "思考中",
  responding: "回复中",
};

interface VoiceOption {
  value: string;
  label: string;
}

function voiceLabel(value: string): string {
  // 线上历史配置可能把女声注册为技术 id "default"，UI 统一展示成「中文女」。
  return value === "default" ? "中文女" : value;
}

function buildVoiceOptions(backendVoices: string[]): VoiceOption[] {
  return backendVoices.map((v) => ({
    value: v,
    label: voiceLabel(v),
  }));
}

export default function ChatPage() {
  const [connected, setConnected] = useState(false);
  const [turnState, setTurnState] = useState<TurnState>("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [userPartial, setUserPartial] = useState("");
  const [assistantPartial, setAssistantPartial] = useState("");
  const [node, setNode] = useState<string | undefined>(undefined);
  const [error, setError] = useState("");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [bargeIn, setBargeIn] = useState(false);
  const [vadGate, setVadGate] = useState(true);
  const [mode, setMode] = useState<ChatMode>("cascade");
  const [model, setModel] = useState("");
  const [models, setModels] = useState<ChatModelInfo[]>([]);
  const [enableThinking, setEnableThinking] = useState(false);
  const [voice, setVoice] = useState("");
  const [voices, setVoices] = useState<VoiceOption[]>([]);

  const wsRef = useRef<WebSocket | null>(null);
  const recRef = useRef<MicRecorder | null>(null);
  const playerRef = useRef<StreamingPcmPlayer | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // 新消息/增量出现时滚到底
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, userPartial, assistantPartial]);

  // 离开页面时收尾
  useEffect(() => () => teardown(), []);

  // 后端按实际 Provider 配置下发模式、模型、音色与能力，前端不硬编码模型名。
  useEffect(() => {
    fetchChatModels()
      .then(({ models: available }) => {
        setModels(available);
        const selected =
          available.find((item) => item.default && item.mode === "native") ||
          available.find((item) => item.mode === "native") ||
          available.find((item) => item.default) ||
          available[0];
        if (selected) {
          applyModel(selected, available);
        } else {
          setError("语音聊天未启用");
        }
      })
      .catch((err) => setError("加载对话模型失败: " + String(err)));
  }, []);

  const selectedModel = models.find(
    (item) => item.mode === mode && item.name === model
  );
  const modeOptions = Array.from(new Set(models.map((item) => item.mode)));

  function applyModel(selected: ChatModelInfo, available = models) {
    setMode(selected.mode);
    setModel(selected.name);
    const options = buildVoiceOptions(selected.voices);
    setVoices(options);
    setVoice((cur) =>
      options.some((opt) => opt.value === cur) ? cur : options[0]?.value || ""
    );
    if (!selected.supports_thinking) setEnableThinking(false);
    if (selected.mode === "native") setBargeIn(true);
    else if (!selected.supports_barge_in) setBargeIn(false);
    if (!selected.supports_vad_gate) setVadGate(false);
    if (!available.some((item) => item.name === selected.name)) {
      setModels(available);
    }
  }

  function selectMode(nextMode: ChatMode) {
    const selected =
      models.find((item) => item.mode === nextMode && item.default) ||
      models.find((item) => item.mode === nextMode);
    if (selected) applyModel(selected);
  }

  function selectModel(name: string) {
    const selected = models.find(
      (item) => item.mode === mode && item.name === name
    );
    if (selected) applyModel(selected);
  }

  function teardown() {
    recRef.current?.stop();
    recRef.current = null;
    try {
      wsRef.current?.close();
    } catch {
      /* ignore */
    }
    wsRef.current = null;
    playerRef.current?.close();
    playerRef.current = null;
  }

  async function start() {
    setError("");
    setMessages([]);
    setUserPartial("");
    setAssistantPartial("");
    setTurnState("");
    if (!selectedModel) {
      setError("没有可用的对话模型");
      return;
    }

    const player = new StreamingPcmPlayer({
      initialBufferMs: 100,
      maxBufferedMs: 120000,
      retainAudio: false,
    });
    playerRef.current = player;

    const ws = openChatStream(
      {
        sampleRate: TARGET_SR,
        language: "auto",
        mode,
        inputFormat: selectedModel.input_format,
        systemPrompt,
        voice,
        bargeIn,
        model,
        enableThinking,
      },
      {
        onReady: (n) => {
          setNode(n);
          setConnected(true);
          startMic(ws);
        },
        onState: (s, n) => {
          setTurnState(s as TurnState);
          if (n) setNode(n);
        },
        onUserPartial: (t) => setUserPartial(t),
        onUserFinal: (t) => {
          setUserPartial("");
          if (t.trim()) setMessages((m) => [...m, { role: "user", text: t }]);
        },
        onAssistantPartial: (t) => setAssistantPartial(t),
        onTtsMeta: (sr) => player.setSampleRate(sr),
        onAudio: (pcm) => player.push(pcm),
        onAssistantDone: (t, qos, n) => {
          player.finishTurn();
          setAssistantPartial("");
          if (t.trim()) setMessages((m) => [...m, { role: "assistant", text: t, qos, node: n }]);
        },
        onInterrupted: () => {
          // barge-in: 用户插话打断了 AI。立即停外放, 把已说出的半句落为定稿气泡。
          player.stop();
          setAssistantPartial((cur) => {
            if (cur.trim()) setMessages((m) => [...m, { role: "assistant", text: cur }]);
            return "";
          });
        },
        onError: (code, message) => {
          if (code === "unavailable") {
            setError("语音聊天未启用 (后端未配置远端 Agent)");
            stop();
          } else if (code === "busy") {
            setError("服务繁忙: " + message);
          } else {
            setError(message);
          }
        },
      }
    );
    wsRef.current = ws;
  }

  async function startMic(ws: WebSocket) {
    const rec = new MicRecorder(
      (pcm) => {
        if (ws.readyState !== WebSocket.OPEN) return;
        const payload =
          selectedModel?.input_format === "pcm_s16le"
            ? floatToInt16(pcm)
            : pcm;
        ws.send(payload.buffer);
      },
      { gateVad: selectedModel?.supports_vad_gate ? vadGate : false }
    );
    recRef.current = rec;
    try {
      await rec.start();
    } catch (err) {
      setError("无法访问麦克风: " + String(err));
      stop();
    }
  }

  function stop() {
    recRef.current?.stop();
    recRef.current = null;
    const ws = wsRef.current;
    const stats = playerRef.current?.getStats();
    try {
      if (ws?.readyState === WebSocket.OPEN && stats) {
        ws.send(
          JSON.stringify({
            type: "client_metric",
            playback_underruns: stats.playbackUnderruns,
            dropped_chunks: stats.droppedChunks,
          })
        );
      }
      ws?.send(JSON.stringify({ type: "end" }));
    } catch {
      /* ignore */
    }
    try {
      ws?.close();
    } catch {
      /* ignore */
    }
    wsRef.current = null;
    playerRef.current?.close();
    playerRef.current = null;
    setConnected(false);
    setTurnState("");
    setUserPartial("");
  }

  const hasConversation =
    messages.length > 0 || userPartial.length > 0 || assistantPartial.length > 0;

  return (
    <div className="panel">
      <div className="chat-toolbar">
        {!connected ? (
          <button className="primary" onClick={start}>
            ● 开始对话
          </button>
        ) : (
          <button className="ghost rec active" onClick={stop}>
            ■ 结束对话
          </button>
        )}
        {connected && (
          <span className={`chat-state ${turnState}`}>
            <span className="chat-state-dot" />
            {STATE_LABEL[turnState] || "聆听中"}
          </span>
        )}
        {node && <span className="chat-node">{node}</span>}
        <button
          className="chat-adv-toggle"
          onClick={() => setShowAdvanced((v) => !v)}
          disabled={connected}
        >
          {showAdvanced ? "收起设置" : "高级设置"}
        </button>
      </div>

      <div className="chat-controls">
        <label className="chat-control">
          <span>对话模式</span>
          <select
            value={mode}
            onChange={(e) => selectMode(e.target.value as ChatMode)}
            disabled={connected || modeOptions.length < 2}
          >
            {modeOptions.map((item) => (
              <option key={item} value={item}>
                {item === "native" ? "原生语音" : "级联"}
              </option>
            ))}
          </select>
        </label>
        <label className="chat-control">
          <span>对话模型</span>
          <select
            value={model}
            onChange={(e) => selectModel(e.target.value)}
            disabled={connected}
          >
            {models
              .filter((item) => item.mode === mode)
              .map((item) => (
                <option key={`${item.mode}:${item.name}`} value={item.name}>
                  {item.label}
                </option>
              ))}
          </select>
        </label>
        <label className="chat-control">
          <input
            type="checkbox"
            checked={enableThinking}
            onChange={(e) => setEnableThinking(e.target.checked)}
            disabled={connected || !selectedModel?.supports_thinking}
          />
          <span>思考模式 (仅混合推理模型生效, 首字延迟更高)</span>
        </label>
        {selectedModel?.preserves_paralinguistics && (
          <span className="chat-capability">保留语气、停顿和非语言声</span>
        )}
      </div>

      {showAdvanced && !connected && (
        <div className="chat-advanced">
          <label>回复音色</label>
          <select
            value={voice}
            onChange={(e) => setVoice(e.target.value)}
            disabled={connected || voices.length === 0}
          >
            {voices.map((v) => (
              <option key={v.value} value={v.value}>
                {v.label}
              </option>
            ))}
          </select>

          <label>系统人设 (可选, 留空用后端默认)</label>
          <textarea
            value={systemPrompt}
            placeholder="例如: 你是一个友好的语音助手，用简短口语化的中文回答。"
            onChange={(e) => setSystemPrompt(e.target.value)}
          />
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              checked={vadGate}
              onChange={(e) => setVadGate(e.target.checked)}
              disabled={!selectedModel?.supports_vad_gate}
            />
            静音门控 (VAD)：级联模式过滤环境噪声；原生语音模式强制关闭
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              checked={bargeIn}
              onChange={(e) => setBargeIn(e.target.checked)}
              disabled={
                !selectedModel?.supports_barge_in || selectedModel?.mode === "native"
              }
            />
            说话打断 (barge-in)：原生模式始终开启，建议戴耳机
          </label>
        </div>
      )}

      {error && (
        <div className="result" style={{ color: "#f87171" }}>
          {error}
        </div>
      )}

      <div className="chat-log" ref={scrollRef}>
        {!hasConversation && (
          <div className="chat-empty">
            点击「开始对话」后直接说话即可，系统会自动识别你的话、调用大模型并用语音回答。
          </div>
        )}
        {messages.map((m, i) => (
          <ChatBubble key={i} msg={m} />
        ))}
        {userPartial && (
          <div className="chat-row user">
            <div className="chat-bubble user partial">{userPartial}</div>
          </div>
        )}
        {assistantPartial && (
          <div className="chat-row assistant">
            <div className="chat-bubble assistant partial">{assistantPartial}</div>
          </div>
        )}
      </div>
    </div>
  );
}

function floatToInt16(pcm: Float32Array): Int16Array {
  const out = new Int16Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) {
    const sample = Math.max(-1, Math.min(1, pcm[i]));
    out[i] = sample < 0 ? sample * 32768 : sample * 32767;
  }
  return out;
}

function ChatBubble({ msg }: { msg: ChatMessage }) {
  return (
    <div className={`chat-row ${msg.role}`}>
      <div className={`chat-bubble ${msg.role}`}>
        {msg.text}
        {msg.role === "assistant" && msg.qos && (
          <div className="chat-qos">
            {msg.node && <span className="chat-qos-node">{msg.node}</span>}
            {fmtMs("首字", msg.qos.first_token_ms)}
            {fmtMs("首声", msg.qos.tts_ttfb_ms)}
            {fmtMs("总耗时", msg.qos.total_ms)}
          </div>
        )}
      </div>
    </div>
  );
}

function fmtMs(label: string, v?: number | null) {
  if (v === null || v === undefined) return null;
  return (
    <span className="chat-qos-item">
      <span className="chat-qos-k">{label}</span>
      <span className="chat-qos-v">{Math.round(v)}ms</span>
    </span>
  );
}

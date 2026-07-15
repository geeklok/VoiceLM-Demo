import { useEffect, useRef, useState } from "react";
import {
  fetchModels,
  fetchTnCategories,
  openTtsStream,
  ttsFile,
  TnCategory,
  TtsQos,
} from "../api/client";
import { StreamingPcmPlayer } from "../audio/player";
import { QosBadge } from "../components/QosBadge";

interface VoiceOption {
  value: string;
  label: string;
}

const FALLBACK_VOICES: VoiceOption[] = [
  { value: "中文女", label: "中文女" },
  { value: "中文男", label: "中文男" },
];

function voiceLabel(value: string): string {
  // 后端真实 CosyVoice 零样本音色常以技术 id "default" 注册；UI 不直接暴露该实现名。
  return value === "default" ? "中文女" : value;
}

function buildVoiceOptions(backendVoices: string[]): VoiceOption[] {
  const options = backendVoices.map((v) => ({
    value: v,
    label: voiceLabel(v),
  }));
  // 线上历史配置可能只注册了技术音色 id "default"。这时仍保留产品侧固定的
  // 「中文女 / 中文男」两个入口，避免模型发现返回单个技术 id 后把「中文男」挤掉。
  // 若后端已真实注册同名音色，则按后端返回为准；这里只补缺失的展示项。
  for (const fallback of FALLBACK_VOICES) {
    if (!options.some((opt) => opt.label === fallback.label)) {
      options.push(fallback);
    }
  }
  return options;
}

export default function TtsPage() {
  const [text, setText] = useState("你好，欢迎使用语音大模型合成服务。");
  const [voice, setVoice] = useState(FALLBACK_VOICES[0].value);
  const [voices, setVoices] = useState<VoiceOption[]>(FALLBACK_VOICES);
  const [speed, setSpeed] = useState(1.0);
  const [tnOptions, setTnOptions] = useState<TnCategory[]>([]);
  const [domainTn, setDomainTn] = useState<string[]>([]);
  const [showTn, setShowTn] = useState(false);
  const [audioUrl, setAudioUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [qos, setQos] = useState<TtsQos | null>(null);
  const playerRef = useRef<StreamingPcmPlayer | null>(null);

  useEffect(() => {
    fetchModels()
      .then((m) => {
        if (m.tts[0]?.languages?.length) {
          const options = buildVoiceOptions(m.tts[0].languages);
          setVoices(options);
          setVoice((cur) =>
            options.some((opt) => opt.value === cur) ? cur : options[0].value
          );
        }
      })
      .catch(() => {});
    // 领域 TN 类别由后端单一维护 (domain_tn.py), 前端动态拉取, 不再硬编码 impl。
    fetchTnCategories()
      .then((opts) => {
        setTnOptions(opts);
        const enabled = new Set(opts.filter((opt) => opt.impl).map((opt) => opt.id));
        setDomainTn((cur) => cur.filter((id) => enabled.has(id)));
      })
      .catch(() => {});
  }, []);

  async function onSynthFile() {
    setBusy(true);
    setError("");
    setStatus("合成中...");
    setAudioUrl("");
    setQos(null);
    try {
      const { blob, qos } = await ttsFile(text, voice, speed, domainTn);
      setAudioUrl(URL.createObjectURL(blob));
      setQos({ ...qos, mode: "file" });
      setStatus("合成完成");
    } catch (err) {
      setError(String(err));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  function onSynthStream() {
    setBusy(true);
    setError("");
    setStatus("流式合成中 (边合成边播)...");
    setAudioUrl("");
    setQos(null);
    const player = new StreamingPcmPlayer();
    playerRef.current = player;
    const t0 = performance.now();
    let firstChunk = true;
    let node: string | undefined;
    openTtsStream(
      text,
      voice,
      speed,
      (sr, n) => {
        player.setSampleRate(sr);
        node = n;
      },
      (pcm) => {
        if (firstChunk) {
          setStatus(`首包延迟 ${Math.round(performance.now() - t0)}ms (客户端侧)`);
          firstChunk = false;
        }
        player.push(pcm);
      },
      (serverQos) => {
        setAudioUrl(URL.createObjectURL(player.toWavBlob()));
        if (serverQos) setQos({ ...serverQos, mode: "stream" });
        else if (node) setQos({ node, mode: "stream" });
        setBusy(false);
      },
      (msg) => {
        setError(msg);
        setBusy(false);
      },
      domainTn
    );
  }

  function toggleTn(id: string) {
    const opt = tnOptions.find((x) => x.id === id);
    if (opt && !opt.impl) return;
    setDomainTn((cur) =>
      cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]
    );
  }

  return (
    <div className="panel">
      <label>合成文本</label>
      <textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={5000} />

      <div className="row">
        <div>
          <label>音色</label>
          <select value={voice} onChange={(e) => setVoice(e.target.value)}>
            {voices.map((v) => (
              <option key={v.value} value={v.value}>{v.label}</option>
            ))}
          </select>
        </div>
        <div>
          <label>语速 ({speed.toFixed(1)}x) · 仅一次性合成生效</label>
          <input
            type="range"
            min={0.5}
            max={2}
            step={0.1}
            value={speed}
            onChange={(e) => setSpeed(parseFloat(e.target.value))}
            style={{ width: "100%" }}
          />
        </div>
      </div>

      {tnOptions.length > 0 && (
        <>
          <button
            type="button"
            className="tn-toggle"
            onClick={() => setShowTn((v) => !v)}
            aria-expanded={showTn}
          >
            <span className={`tn-caret${showTn ? " open" : ""}`}>▶</span>
            领域 TN（送模型前的文本归一化预处理，可多选）
            {domainTn.length > 0 && <span className="tn-count">已选 {domainTn.length}</span>}
          </button>
          {showTn && (
            <div className="tn-grid">
              {tnOptions.map((opt) => {
                const disabled = !opt.impl;
                return (
                  <label
                    key={opt.id}
                    className={`tn-item${disabled ? " experimental disabled" : ""}`}
                    title={disabled ? "暂未接入词典/规则，当前不可选择" : "已实现，勾选即生效"}
                    aria-disabled={disabled}
                  >
                    <input
                      type="checkbox"
                      disabled={disabled}
                      checked={!disabled && domainTn.includes(opt.id)}
                      onChange={() => toggleTn(opt.id)}
                    />
                    <span>{opt.label}</span>
                    {disabled && <span className="tn-tag">实验性</span>}
                  </label>
                );
              })}
            </div>
          )}
        </>
      )}

      <div className="tts-actions">
        <button className="primary" onClick={onSynthFile} disabled={busy || !text.trim()}>
          一次性合成
        </button>
        <button className="ghost" onClick={onSynthStream} disabled={busy || !text.trim()}>
          流式合成
        </button>
      </div>

      {error && <div className="result" style={{ color: "#f87171" }}>{error}</div>}
      {status && <div className="meta">{status}</div>}
      {qos && (
        <QosBadge
          node={qos.node}
          model={qos.model}
          mode={qos.mode}
          ttfbMs={qos.ttfb_ms}
          processMs={qos.process_ms}
          audioMs={qos.audio_ms}
          rtf={qos.rtf}
        />
      )}
      {audioUrl && (
        <>
          <audio controls src={audioUrl} autoPlay />
          <div className="meta">
            <a href={audioUrl} download="tts.wav" style={{ color: "var(--accent)" }}>
              下载音频
            </a>
          </div>
        </>
      )}
    </div>
  );
}

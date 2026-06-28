interface QosBadgeProps {
  node?: string;
  model?: string;
  mode?: string;
  ttfbMs?: number | null;
  processMs?: number | null;
  audioMs?: number | null;
  rtf?: number | null;
  degraded?: boolean;
}

// 灰度对比: 入口机 node1 已应用 B 线优化, 扩展机 node2 为旧版基线。
// 用不同色块让用户一眼看出本次请求落在哪台机器。
function nodeColor(node?: string): string {
  if (!node) return "#64748b";
  if (node === "node1") return "#22c55e"; // 优化版 (入口机)
  if (node === "node2") return "#f59e0b"; // 基线版 (扩展机)
  return "#38bdf8";
}

function fmtMs(v?: number | null): string | null {
  return v === null || v === undefined ? null : `${Math.round(v)}ms`;
}

function fmtRtf(v?: number | null): string | null {
  return v === null || v === undefined ? null : v.toFixed(3);
}

export function QosBadge(props: QosBadgeProps) {
  const { node, model, mode, ttfbMs, processMs, audioMs, rtf, degraded } = props;
  const items: Array<[string, string]> = [];
  if (mode) items.push(["方式", mode === "stream" ? "流式" : "一次性"]);
  if (model) items.push(["模型", model]);
  const ttfb = fmtMs(ttfbMs);
  if (ttfb) items.push(["首包", ttfb]);
  const proc = fmtMs(processMs);
  if (proc) items.push(["处理", proc]);
  const audio = fmtMs(audioMs);
  if (audio) items.push(["音频", audio]);
  const rtfStr = fmtRtf(rtf);
  if (rtfStr) items.push(["RTF", rtfStr]);

  return (
    <div className="qos-badge">
      <span className="qos-node" style={{ background: nodeColor(node) }}>
        {node || "unknown"}
        {node === "node1" && " · 优化版"}
        {node === "node2" && " · 基线版"}
      </span>
      {degraded && <span className="qos-degraded">已降级</span>}
      {items.map(([k, v]) => (
        <span key={k} className="qos-item">
          <span className="qos-k">{k}</span>
          <span className="qos-v">{v}</span>
        </span>
      ))}
    </div>
  );
}

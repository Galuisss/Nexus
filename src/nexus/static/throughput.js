const encoder = new TextEncoder();
const windowMs = 3000;

export function enabled(settings, turn) {
  return settings?.summary === 'none' && !(turn?.items || []).some(item =>
    item.type === 'reasoning' && item.summary?.some(part => typeof part === 'string' ? part : part.text));
}

export function recordDelta(item, delta, now = performance.now()) {
  if (!delta) return;
  const meter = item._throughput ||= { bytes: 0, samples: [], firstAt: now, active: true };
  meter.bytes += encoder.encode(delta).length;
  meter.samples.push({ at: now, tokens: meter.bytes / 4 });
  trimSamples(meter.samples, now);
}

function trimSamples(samples, now) {
  while (samples.length > 1 && samples[1].at <= now - windowMs) samples.shift();
}

export function finish(item, now = performance.now()) {
  if (!item._throughput?.active) return;
  item._throughput.active = false;
  item._throughput.durationMs = now - item._throughput.firstAt;
}

export function live(turn, now = performance.now()) {
  const item = [...(turn?.items || [])].reverse().find(item => item.type === 'agentMessage' && item._throughput?.active && item._active !== false);
  if (!item) return null;
  const { samples } = item._throughput;
  trimSamples(samples, now);
  const first = samples[0];
  const start = Math.max(now - windowMs, first.at);
  const duration = now - start;
  if (duration < 500) return null;
  let baseline = first.tokens;
  if (first.at < start && samples.length > 1) {
    const next = samples[1];
    baseline += (next.tokens - first.tokens) * (start - first.at) / (next.at - first.at);
  }
  return { tps: (samples.at(-1).tokens - baseline) * 1000 / duration, estimated: true };
}

export function average(turn) {
  const messages = (turn?.items || []).filter(item => item.type === 'agentMessage' && (item.text || item.nexusTps));
  if (!messages.length) return null;
  let tokens = 0;
  let durationMs = 0;
  let estimated = false;
  for (const item of messages) {
    const metric = item.nexusTps;
    const meter = item._throughput;
    if (metric) {
      tokens += metric.tokens;
      durationMs += metric.durationMs;
    } else if (meter && !meter.active && meter.durationMs >= 500) {
      tokens += meter.bytes / 4;
      durationMs += meter.durationMs;
      estimated = true;
    }
  }
  return durationMs > 0 ? { tps: tokens * 1000 / durationMs, estimated } : null;
}

export function format(metric) {
  return metric ? `${metric.estimated ? '≈ ' : ''}${metric.tps.toFixed(1)} TPS` : '';
}

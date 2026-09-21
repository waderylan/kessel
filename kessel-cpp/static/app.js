const form = document.querySelector("#prompt-form");
const model = document.querySelector("#model");
const key = document.querySelector("#api-key");
const prompt = document.querySelector("#prompt");
const responseText = document.querySelector("#response");
const empty = document.querySelector("#empty");
const loading = document.querySelector("#loading");
const requestError = document.querySelector("#request-error");
const submit = document.querySelector("#submit");
const responseMeta = document.querySelector("#response-meta");
const copy = document.querySelector("#copy");
const fast = document.querySelector("#fast");
const warm = document.querySelector("#warm");
const endpoint = document.querySelector("#endpoint");

function provider() { return form.elements.provider.value; }
function show(name) {
  empty.hidden = name !== "empty";
  loading.hidden = name !== "loading";
  responseText.hidden = name !== "response";
  requestError.hidden = name !== "error";
  responseMeta.hidden = name !== "response";
  copy.hidden = name !== "response";
}
async function models() {
  const headers = key.value ? { Authorization: `Bearer ${key.value}` } : {};
  try {
    const reply = await fetch(`/v1/${provider()}/models`, { headers });
    if (!reply.ok) throw new Error();
    const body = await reply.json();
    model.replaceChildren(...[{ id: "default" }, ...(body.data || [])].map((item) => {
      const option = document.createElement("option"); option.value = item.id; option.textContent = item.id === "default" ? "Default (account selection)" : item.id; return option;
    }));
  } catch { model.innerHTML = '<option value="default">Default (model list unavailable)</option>'; }
}
function usageText(usage, started) {
  const elapsed = ((performance.now() - started) / 1000).toFixed(1);
  if (!usage) return `${provider()} completed in ${elapsed}s`;
  const cached = usage.prompt_tokens_details?.cached_tokens || 0;
  return `${provider()} completed in ${elapsed}s, ${Math.max(0, usage.prompt_tokens - cached)} new input, ${usage.completion_tokens} output${cached ? `, ${cached} cached` : ""}`;
}
async function streamReply(reply, started) {
  const reader = reply.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; let output = ""; show("response");
  let usage = null;
  while (true) {
    const { value, done } = await reader.read(); buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split("\n\n"); buffer = frames.pop() || "";
    for (const frame of frames) {
      const line = frame.split("\n").find((item) => item.startsWith("data: ")); if (!line || line === "data: [DONE]") continue;
      const event = JSON.parse(line.slice(6)); if (event.error) throw new Error(event.error.message); if (event.usage) usage = event.usage; const delta = event.choices?.[0]?.delta;
      if (delta?.content) { output += delta.content; responseText.textContent = output; }
      if (delta?.tool_calls) responseText.textContent = JSON.stringify(delta.tool_calls[0], null, 2);
    }
    if (done) break;
  }
  if (!output && !responseText.textContent) responseText.textContent = "Provider returned no output.";
  responseMeta.textContent = usageText(usage, started);
}
form.addEventListener("change", (event) => { if (event.target.name === "provider") { const codex = provider() === "codex"; document.querySelector("#warm-label").hidden = !codex; document.querySelector("#fast-label").hidden = !codex; if (!codex) { warm.checked = false; fast.checked = false; } endpoint.textContent = `${window.location.origin}/v1/${provider()}`; models(); } });
key.addEventListener("change", models);
form.addEventListener("submit", async (event) => {
  event.preventDefault(); submit.disabled = true; responseText.textContent = ""; responseMeta.textContent = ""; show("loading");
  const started = performance.now();
  try {
    const streaming = document.querySelector("#stream").checked;
    const reply = await fetch(`/v1/${provider()}/chat/completions`, { method: "POST", headers: { "Content-Type": "application/json", ...(key.value ? { Authorization: `Bearer ${key.value}` } : {}) }, body: JSON.stringify({ model: model.value, reasoning_effort: document.querySelector("#effort").value, service_tier: fast.checked ? "fast" : "default", backend: warm.checked ? "warm" : "fresh", stream: streaming, stream_options: { include_usage: true }, messages: [{ role: "user", content: prompt.value }] }) });
    if (!reply.ok) { const body = await reply.json(); throw new Error(body.error?.message || "Request failed"); }
    if (streaming) await streamReply(reply, started); else { const body = await reply.json(); responseText.textContent = body.choices[0].message.content || JSON.stringify(body.choices[0].message.tool_calls, null, 2); show("response"); responseMeta.textContent = usageText(body.usage, started); }
  } catch (error) { requestError.textContent = error.message || "Request failed"; show("error"); }
  finally { submit.disabled = false; }
});
copy.addEventListener("click", async () => { try { await navigator.clipboard.writeText(responseText.textContent); copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy"; }, 1200); } catch { copy.textContent = "Copy failed"; } });
fetch("/health").then((reply) => reply.json()).then((body) => { document.querySelector("#status").textContent = Object.entries(body.providers).filter(([, value]) => value.available).map(([name]) => name).join(" + ") + " available"; }).catch(() => { document.querySelector("#status").textContent = "API unavailable"; });
models();

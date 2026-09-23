const form = document.querySelector("#prompt-form");
const promptInput = document.querySelector("#prompt");
const modelInput = document.querySelector("#model");
const modelHelp = document.querySelector("#model-help");
const effortInput = document.querySelector("#reasoning-effort");
const fastTierInput = document.querySelector("#fast-tier");
const fastTierField = document.querySelector("#fast-tier-field");
const warmInput = document.querySelector("#warm-backend");
const warmField = document.querySelector("#warm-field");
const streamInput = document.querySelector("#stream-response");
const apiKeyInput = document.querySelector("#api-key");
const accountStatus = document.querySelector("#account-status");
const submitButton = document.querySelector("#submit-button");
const formError = document.querySelector("#form-error");
const serviceStatus = document.querySelector("#service-status");
const endpointCode = document.querySelector("#endpoint-code");
const copyButton = document.querySelector("#copy-button");
const responseText = document.querySelector("#response-text");
const responseMeta = document.querySelector("#response-meta");
const requestError = document.querySelector("#request-error");
const selectedModels = { codex: "default", claude: "default" };
const providerNames = { codex: "Codex", claude: "Claude Code" };
let providerAccounts = {};
let modelRequestSequence = 0;
let accountRequestSequence = 0;

const states = {
  empty: document.querySelector("#empty-state"),
  loading: document.querySelector("#loading-state"),
  response: document.querySelector("#response-state"),
  error: document.querySelector("#error-state"),
};

function selectedProvider() {
  return form.elements.provider.value;
}

function showState(name) {
  Object.entries(states).forEach(([stateName, element]) => {
    element.hidden = stateName !== name;
  });
  copyButton.hidden = name !== "response";
}

function updateEndpoint() {
  const provider = selectedProvider();
  endpointCode.textContent = `${window.location.origin}/v1/${provider}`;
  fastTierField.hidden = provider !== "codex";
  warmField.hidden = provider !== "codex";
  if (provider !== "codex") fastTierInput.checked = false;
  if (provider !== "codex") warmInput.checked = false;
  renderAccount(provider);
  loadModels(provider);
}

function renderAccount(provider) {
  const account = providerAccounts[provider];
  const name = providerNames[provider] || provider;
  if (!account) {
    accountStatus.textContent = "Account details are unavailable.";
    return;
  }
  if (account.status === "not_installed") {
    accountStatus.textContent = `${name} is not installed.`;
    return;
  }
  if (account.status === "not_authenticated") {
    accountStatus.textContent = `${name} is not signed in.`;
    return;
  }
  if (account.status !== "authenticated") {
    accountStatus.textContent = `${name} account details are unavailable.`;
    return;
  }

  const identity = account.email || account.organization || "Signed in";
  const details = [account.organization, account.subscription, account.auth_method]
    .filter((value) => value && value !== identity);
  accountStatus.textContent = [identity, ...details].join(" · ");
}

async function loadAccounts() {
  const requestSequence = ++accountRequestSequence;
  providerAccounts = {};
  accountStatus.textContent = "Checking selected provider account.";
  const headers = {};
  if (apiKeyInput.value) {
    headers.Authorization = `Bearer ${apiKeyInput.value}`;
  }
  try {
    const response = await fetch("/v1/providers/accounts", { headers });
    if (requestSequence !== accountRequestSequence) return;
    if (!response.ok) {
      if (response.status === 401) {
        accountStatus.textContent =
          "Enter the local API key to view account details.";
        return;
      }
      throw new Error("Account lookup failed");
    }
    const data = await response.json();
    if (requestSequence !== accountRequestSequence) return;
    providerAccounts = Object.fromEntries(
      (data.data || []).map((account) => [account.provider, account]),
    );
    renderAccount(selectedProvider());
  } catch {
    if (requestSequence !== accountRequestSequence) return;
    providerAccounts = {};
    accountStatus.textContent = "Account details are unavailable.";
  }
}

function replaceModelOptions(models, selectedModel) {
  modelInput.replaceChildren();
  models.forEach(({ value, label }) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    modelInput.append(option);
  });
  modelInput.value = models.some(({ value }) => value === selectedModel)
    ? selectedModel
    : "default";
}

async function loadModels(provider, preferredModel = selectedModels[provider]) {
  const requestSequence = ++modelRequestSequence;
  modelInput.disabled = true;
  modelInput.setAttribute("aria-busy", "true");
  replaceModelOptions(
    [{ value: "default", label: "Loading models..." }],
    "default",
  );
  modelHelp.textContent = "Loading models from the selected local login.";

  try {
    const headers = {};
    if (apiKeyInput.value) {
      headers.Authorization = `Bearer ${apiKeyInput.value}`;
    }
    const response = await fetch(`/v1/${provider}/models`, { headers });
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.error?.message || "Model discovery failed");
    }
    const data = await response.json();
    if (requestSequence !== modelRequestSequence || provider !== selectedProvider()) {
      return;
    }
    const discoveredModels = (data.data || [])
      .map((model) => model.id)
      .filter((model) => model && model !== "default");
    replaceModelOptions(
      [
        { value: "default", label: "Default (account selection)" },
        ...discoveredModels.map((model) => ({ value: model, label: model })),
      ],
      preferredModel,
    );
    modelHelp.textContent =
      provider === "codex"
        ? "Models available to the current Codex login."
        : discoveredModels.length
          ? "Models confirmed by Claude Code during this server session."
          : "Default uses the account model. Confirmed model IDs appear after use.";
  } catch (error) {
    if (requestSequence !== modelRequestSequence || provider !== selectedProvider()) {
      return;
    }
    replaceModelOptions(
      [{ value: "default", label: "Default (model list unavailable)" }],
      "default",
    );
    modelHelp.textContent = error.message || "Could not load local models.";
  } finally {
    if (requestSequence === modelRequestSequence && provider === selectedProvider()) {
      modelInput.disabled = false;
      modelInput.setAttribute("aria-busy", "false");
    }
  }
}

function formatUsage(usage, provider, elapsedSeconds) {
  const elapsed = `${elapsedSeconds.toFixed(1)}s`;
  if (!usage) return `${provider} completed in ${elapsed}`;
  const cached = usage.prompt_tokens_details?.cached_tokens || 0;
  const freshInput = Math.max(0, usage.prompt_tokens - cached);
  const parts = [
    `${freshInput.toLocaleString()} new input`,
    `${usage.completion_tokens.toLocaleString()} output`,
  ];
  if (cached) parts.push(`${cached.toLocaleString()} cached`);
  return `${provider} completed in ${elapsed}, ${parts.join(", ")}`;
}

async function checkHealth() {
  try {
    const response = await fetch("/health");
    if (!response.ok) throw new Error("Health check failed");
    const data = await response.json();
    if (data.status === "degraded") {
      serviceStatus.textContent = data.message || "No provider commands found";
      return;
    }
    const available = Object.entries(data.providers)
      .filter(([, status]) => status.available)
      .map(([name]) => name);
    serviceStatus.textContent = available.length
      ? `${available.join(" + ")} available`
      : "No provider commands found";
  } catch {
    serviceStatus.textContent = "API unavailable";
  }
}

async function readOpenAIStream(response, provider, startedAt) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let output = "";
  let usage = null;
  let toolCall = null;
  showState("response");
  responseMeta.textContent = `${provider} streaming`;

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const dataLine = frame
        .split("\n")
        .find((line) => line.startsWith("data: "));
      if (!dataLine || dataLine === "data: [DONE]") continue;
      const event = JSON.parse(dataLine.slice(6));
      if (event.error) throw new Error(event.error.message || "Stream failed");
      if (event.usage) usage = event.usage;
      const delta = event.choices?.[0]?.delta;
      if (delta?.content) {
        output += delta.content;
        responseText.textContent = output;
      }
      if (delta?.tool_calls?.[0]) {
        toolCall = delta.tool_calls[0];
        responseText.textContent = JSON.stringify(toolCall, null, 2);
      }
    }
    if (done) break;
  }

  if (!output && !toolCall) responseText.textContent = "Provider returned no output.";
  const elapsedSeconds = (performance.now() - startedAt) / 1000;
  responseMeta.textContent = formatUsage(usage, provider, elapsedSeconds);
}

form.addEventListener("change", (event) => {
  if (event.target.name === "provider") updateEndpoint();
  if (event.target.name === "model") {
    selectedModels[selectedProvider()] = modelInput.value;
  }
});

apiKeyInput.addEventListener("change", () => {
  loadAccounts();
  loadModels(selectedProvider());
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  formError.textContent = "";

  const prompt = promptInput.value.trim();
  const model = modelInput.value.trim();
  if (!prompt) {
    formError.textContent = "Enter a request.";
    promptInput.focus();
    return;
  }
  if (!model) {
    formError.textContent = "Choose a model.";
    modelInput.focus();
    return;
  }

  const provider = selectedProvider();
  submitButton.disabled = true;
  submitButton.textContent = "Running provider";
  showState("loading");
  const startedAt = performance.now();

  try {
    const headers = { "Content-Type": "application/json" };
    if (apiKeyInput.value) {
      headers.Authorization = `Bearer ${apiKeyInput.value}`;
    }
    const response = await fetch(`/v1/${provider}/chat/completions`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        model,
        reasoning_effort: effortInput.value,
        service_tier: fastTierInput.checked ? "fast" : "default",
        backend: warmInput.checked ? "warm" : "fresh",
        stream: streamInput.checked,
        stream_options: { include_usage: true },
        messages: [{ role: "user", content: prompt }],
      }),
    });
    if (!response.ok) {
      const data = await response.json();
      throw new Error(data.error?.message || data.detail || "Request failed");
    }

    if (streamInput.checked) {
      await readOpenAIStream(response, provider, startedAt);
      loadModels(provider, model);
      return;
    }

    const data = await response.json();
    responseText.textContent = data.choices[0].message.content;
    const elapsedSeconds = (performance.now() - startedAt) / 1000;
    responseMeta.textContent = formatUsage(data.usage, provider, elapsedSeconds);
    showState("response");
    loadModels(provider, model);
  } catch (error) {
    requestError.textContent = error.message || "The local request failed.";
    showState("error");
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "Send request";
  }
});

copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(responseText.textContent);
    copyButton.textContent = "Copied";
    window.setTimeout(() => {
      copyButton.textContent = "Copy";
    }, 1200);
  } catch {
    copyButton.textContent = "Copy failed";
  }
});

updateEndpoint();
loadAccounts();
checkHealth();

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const form = document.getElementById("check-form");
const pdfBtn = document.getElementById("pdf-btn");
const errorBox = document.getElementById("error");
const resultBox = document.getElementById("result");
const searchForm = document.getElementById("search-form");
const searchResult = document.getElementById("search-result");
const busy = document.getElementById("busy");
const machine = document.getElementById("machine");

let lastFile = null;

function showError(message) {
  errorBox.hidden = false;
  errorBox.textContent = message;
}

function clearError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

function paramsFromForm() {
  const data = new FormData(form);
  const query = new URLSearchParams();
  query.set("on", data.get("on"));
  if (data.get("quote_norms")) query.set("quote_norms", "true");
  return query;
}

function findingsHtml(title, items) {
  if (!items || !items.length) return "";
  const blocks = items.map((item) => {
    const clauses = item.clauses && item.clauses.length
      ? `<p>пункты договора: ${esc(item.clauses.join(", "))}</p>`
      : "";
    const norms = item.norms && item.norms.length
      ? `<p>норма: ${esc(item.norms.join("; "))}</p>`
      : "";
    const evidence = item.evidence ? `<p>${esc(item.evidence)}</p>` : "";
    const rec = item.recommendation ? `<p>редакция: ${esc(item.recommendation)}</p>` : "";
    return `<article class="finding"><p><strong>[${esc(item.code)}]</strong> ${esc(item.title)}</p>${evidence}${clauses}${norms}${rec}</article>`;
  });
  return `<h2>${title}</h2>${blocks.join("")}`;
}

function assessmentHtml(item) {
  const group = (head, lines, cls) => {
    if (!lines || !lines.length) return "";
    const rows = lines.map((line) => `<p class="${cls}">— ${esc(line)}</p>`).join("");
    return `<p class="subhead">${esc(head)}</p>${rows}`;
  };
  return [
    `<p class="subject">${esc(item.subject || "")}</p>`,
    `<p class="verdict">${esc(item.headline || "")}</p>`,
    group("Установлено", item.established, ""),
    group("Обращает на себя внимание", item.concerns, "warn"),
    group("Не установлено", item.gaps, "warn"),
    group("Проверить до подписания", item.manual, ""),
  ].join("");
}

function walletHtml(scores, analysis) {
  if (!scores || !scores.length) {
    return `<h2>Блок 2. Адрес расчёта</h2><p>В тексте нет адреса. Оценка не выполнялась.</p>`;
  }
  const blocks = (analysis || []).map((item, index) => {
    const score = scores[index] || {};
    const notes = (score.source_notes || []).map((note) => `<p>${esc(note)}</p>`).join("");
    const disclaimer = `<p class="quote">${esc(score.disclaimer || "")}</p>`;
    return `<article class="finding">${assessmentHtml(item)}${notes}${disclaimer}</article>`;
  }).join("");
  return `<h2>Блок 2. Адрес расчёта</h2>${blocks}`;
}

function partyHtml(parties, analysis) {
  if (!parties || !parties.length) {
    return `<h2>Блок 3. Стороны и иные лица, названные в договоре</h2><p>сверка не выполнена</p>`;
  }
  const blocks = (analysis || [])
    .map((item) => `<article class="finding">${assessmentHtml(item)}</article>`)
    .join("");
  return `<h2>Блок 3. Стороны и иные лица, названные в договоре</h2>${blocks}`;
}
  const blocks = parties.map((item) => {
    const role = item.role ? ` — ${esc(item.role)}` : "";
    const who = esc(item.name || item.inn || "сторона не названа") + role;
    const inn = item.inn ? `<p>ИНН ${esc(item.inn)}</p>` : "";
    const foreign = item.foreign ? `<p>иностранная сторона</p>` : "";
    const hits = (item.hits || []).map((hit) => {
      const markers = (hit.markers || [])
        .map((marker) => `<p class="warn">— ${esc(marker)}</p>`)
        .join("");
      return `<p>${esc(hit.detail || hit.source)}</p>${markers}`;
    }).join("");
    return `<article class="finding"><p>${who}</p>${foreign}${inn}<p>${esc(item.summary || "")}</p>${hits}</article>`;
  }).join("");
  return `<h2>3. Лица, названные в договоре</h2>${blocks}`;
}

function llmHtml(llm) {
  if (!llm) return "";
  const notes = (llm.notes || []).map((note) => {
    const mark = note.present === true ? "есть в тексте" : note.present === false ? "в тексте не видно" : "не ясно";
    const quote = note.quote ? `<p>цитата: ${esc(note.quote)}</p>` : "";
    const reading = note.reading ? `<p>${esc(note.reading)}</p>` : "";
    return `<article class="finding"><p>[${esc(note.code)}] ${esc(mark)}</p>${quote}${reading}</article>`;
  }).join("");
  // Имя модели показываем, только если она отвечала: иначе строка
  // «модель: llama3.3:70b» под сообщением «Ollama недоступна» читается так,
  // будто разбор всё-таки был.
  const tier = llm.tier || {};
  const label = tier.label ? ` (${esc(tier.label)})` : "";
  const model = llm.available && llm.model
    ? `<p>модель: ${esc(llm.model)}${label}</p>`
    : "";
  const warn = llm.available && tier.note && ["limited", "unsupported", "unknown"].includes(tier.tier)
    ? `<p class="warn">${esc(tier.note)}</p>`
    : "";
  const coverage = llm.coverage || {};
  const partial = llm.available && coverage.summary && coverage.complete === false
    ? `<p class="warn">Покрытие: ${esc(coverage.summary)}.</p>`
    : "";
  const extra = notes ? `<h2>Оговорки, которые формальная проверка не ловит</h2>${notes}` : "";
  return `<p>${esc(llm.detail || "")}</p>${model}${warn}${partial}${extra}`;
}

function renderReport(payload) {
  resultBox.hidden = false;
  resultBox.innerHTML = `
    <p class="status ${esc(payload.status)}">${esc(payload.status_label)}</p>
    <p>Документ: ${esc(payload.source)}. Проверено на дату: ${esc(payload.checked_on)}.</p>
    <p>Итого правил: ${payload.counts.total}. Выполнено: ${payload.counts.passed}.
       Нарушено: ${payload.counts.failed}. На ручной оценке: ${payload.counts.manual}.</p>
    ${llmHtml(payload.llm)}
    <h2>1. Договор</h2>
    ${findingsHtml("Нарушены обязательные требования", payload.blocking)}
    ${findingsHtml("Замечания", payload.advisory)}
    ${findingsHtml("Нормы, вступающие в силу позднее", payload.deferred)}
    ${findingsHtml("Требует оценки юриста", payload.manual)}
    ${walletHtml(payload.address_scores, payload.wallet_analysis)}
    ${partyHtml(payload.counterparties, payload.party_analysis)}
  `;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  const data = new FormData(form);
  const file = data.get("file");
  if (!(file instanceof File) || !file.size) {
    showError("выберите файл контракта");
    return;
  }
  lastFile = file;
  pdfBtn.disabled = true;
  resultBox.hidden = true;
  busy.hidden = false;

  const body = new FormData();
  body.append("file", file, file.name);

  try {
    const response = await fetch(`/check?${paramsFromForm().toString()}`, {
      method: "POST",
      body,
    });
    const payload = await response.json();
    if (!response.ok) {
      showError(payload.detail || "проверка не выполнена");
      return;
    }
    renderReport(payload);
    pdfBtn.disabled = false;
  } catch (error) {
    showError("нет связи с сервером");
  } finally {
    busy.hidden = true;
  }
});

pdfBtn.addEventListener("click", async () => {
  if (!lastFile) return;
  clearError();
  busy.hidden = false;
  const body = new FormData();
  body.append("file", lastFile, lastFile.name);
  try {
    const response = await fetch(`/check/pdf?${paramsFromForm().toString()}`, {
      method: "POST",
      body,
    });
    if (!response.ok) {
      showError("PDF не сформирован");
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "zaklyuchenie.pdf";
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    showError("нет связи с сервером");
  } finally {
    busy.hidden = true;
  }
});

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = new FormData(searchForm).get("q");
  searchResult.textContent = "";
  try {
    const response = await fetch(`/search?q=${encodeURIComponent(query)}`);
    const payload = await response.json();
    if (!response.ok) {
      searchResult.textContent = payload.detail || "поиск не выполнен";
      return;
    }
    if (!payload.hits.length) {
      searchResult.textContent = "Ничего не найдено.";
      return;
    }
    searchResult.innerHTML = payload.hits.map(
      (hit) => `<article class="search-hit"><p>${esc(hit.ref)}</p><p>${esc(hit.text)}</p></article>`
    ).join("");
  } catch (error) {
    searchResult.textContent = "нет связи с сервером";
  }
});

fetch("/health")
  .then((response) => response.json())
  .then((payload) => {
    if (!machine) return;
    if (payload.ollama === "off") {
      machine.hidden = false;
      machine.textContent =
        "Локальная Ollama сейчас не отвечает. Договор проверяется по матрице правил; смысл нестандартных оговорок модель не разбирает. Кошелёк и контрагент запрашиваются как обычно.";
    }
  })
  .catch(() => {});

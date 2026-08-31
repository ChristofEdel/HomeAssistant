const RECORDER_HISTORY_RESULT_EVENT = "recorder-history-query-result";
const RECORDER_HISTORY_STYLESHEET = "/local/recorder-history-query-card.css";

function recorderHistoryStylesheetLink() {
  return `<link rel="stylesheet" href="${RECORDER_HISTORY_STYLESHEET}">`;
}

class RecorderHistoryQueryButtonsCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = {};
    this._busy = false;
  }

  setConfig(config) {
    this._config = {
      entity: "select.recorder_history_entity",
      script: "script.get_entity_history",
      ...config,
    };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._updateDisabledState();
  }

  getCardSize() {
    return 3;
  }

  _render() {
    this.shadowRoot.innerHTML = `
      ${recorderHistoryStylesheetLink()}
      <ha-card class="query-buttons-card">
        <div class="buttons">
          <button type="button" data-history-type="recorder"><ha-icon icon="mdi:database-clock"></ha-icon><span>Get Recorder History</span></button>
          <button type="button" data-history-type="short_term"><ha-icon icon="mdi:chart-timeline-variant-shimmer"></ha-icon><span>Get Short-Term Statistics</span></button>
          <button type="button" data-history-type="long_term"><ha-icon icon="mdi:chart-timeline-variant"></ha-icon><span>Get Long-Term Statistics</span></button>
        </div>
      </ha-card>
    `;

    this.shadowRoot.querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => this._run(button.dataset.historyType));
    });

    this._updateDisabledState();
  }

  _selectedEntity() {
    if (!this._hass) {
      return null;
    }

    const state = this._hass.states[this._config.entity];
    if (!state || ["", "unknown", "unavailable"].includes(state.state)) {
      return null;
    }

    return state.state;
  }

  _updateDisabledState() {
    if (!this.shadowRoot) {
      return;
    }

    const disabled = this._busy || !this._selectedEntity();
    this.shadowRoot.querySelectorAll("button").forEach((button) => {
      button.disabled = disabled;
    });
  }

  _dispatch(detail) {
    window.dispatchEvent(
      new CustomEvent(RECORDER_HISTORY_RESULT_EVENT, {
        detail,
      }),
    );
  }

  async _run(historyType) {
    const entityId = this._selectedEntity();
    if (!entityId || !this._hass || this._busy) {
      return;
    }

    const [domain, service] = this._config.script.split(".", 2);
    if (!domain || !service) {
      this._dispatch({
        status: "error",
        message: `Invalid script action: ${this._config.script}`,
      });
      return;
    }

    this._busy = true;
    this._updateDisabledState();
    this._dispatch({
      status: "loading",
      entityId,
      historyType,
    });

    try {
      const serviceResult = await this._hass.callService(
        domain,
        service,
        {
          entity_id: entityId,
          history_type: historyType,
        },
        undefined,
        true,
        true,
      );

      const rows = serviceResult?.response?.result;
      if (!Array.isArray(rows)) {
        throw new Error("The action did not return a result list.");
      }

      this._dispatch({
        status: "success",
        entityId,
        historyType,
        rows,
      });
    } catch (error) {
      this._dispatch({
        status: "error",
        entityId,
        historyType,
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      this._busy = false;
      this._updateDisabledState();
    }
  }
}

class RecorderHistoryQueryResultCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = {};
    this._renderToken = 0;
    this._eventHandler = (event) => this._handleResult(event.detail);
  }

  setConfig(config) {
    this._config = config || {};
    this._renderShell();
  }

  set hass(hass) {
    this._hass = hass;
  }

  connectedCallback() {
    window.addEventListener(RECORDER_HISTORY_RESULT_EVENT, this._eventHandler);
  }

  disconnectedCallback() {
    window.removeEventListener(RECORDER_HISTORY_RESULT_EVENT, this._eventHandler);
    this._renderToken += 1;
  }

  getCardSize() {
    return 10;
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      ${recorderHistoryStylesheetLink()}
      <ha-card class="query-result-card">
        <div class="header-row">
          <div class="header">Recorder History</div>
          <button class="copy-button" type="button" title="Copy to Clipboard" aria-label="Copy to Clipboard" disabled>
            <ha-icon icon="mdi:clipboard-plus-outline"></ha-icon>
          </button>
        </div>
        <div class="status">Select an entity and query type.</div>
        <div class="table-wrap"></div>
      </ha-card>
    `;

    this.shadowRoot.querySelector(".copy-button")
      .addEventListener("click", () => this._copyTable());
  }

  async _copyTable() {
    const table = this.shadowRoot.querySelector("table");
    if (!table) {
      return;
    }

    const text = Array.from(table.rows)
      .map((row) => Array.from(row.cells).map((cell) => cell.innerText).join("\t"))
      .join("\n");

    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        return;
      }
    } catch (_error) {
      // Fall back below for browsers/WebViews where Clipboard API is blocked.
    }

    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    textarea.style.top = "0";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    textarea.setSelectionRange(0, textarea.value.length);

    try {
      document.execCommand("copy");
    } finally {
      textarea.remove();
    }
  }

  _handleResult(detail) {
    this._renderToken += 1;
    const token = this._renderToken;

    const header = this.shadowRoot.querySelector(".header");
    const status = this.shadowRoot.querySelector(".status");
    const tableWrap = this.shadowRoot.querySelector(".table-wrap");
    const copyButton = this.shadowRoot.querySelector(".copy-button");
    tableWrap.replaceChildren();
    copyButton.disabled = true;
    status.classList.remove("error");
    status.hidden = false;

    if (!detail || detail.status === "loading") {
      const title = this._title(detail?.historyType);
      header.textContent = detail?.entityId ? `${title} — ${detail.entityId}` : title;
      status.textContent = "Loading…";
      return;
    }

    if (detail.status === "error") {
      const title = this._title(detail.historyType);
      header.textContent = detail.entityId ? `${title} — ${detail.entityId}` : title;
      status.textContent = detail.message || "Query failed.";
      status.classList.add("error");
      return;
    }

    if (detail.status !== "success") {
      return;
    }

    const rows = detail.rows || [];
    const title = this._title(detail.historyType);
    const unit = detail.historyType === "recorder" ? null : rows[0]?.unit_of_measurement;

    header.textContent = `${title} — ${detail.entityId} — ${rows.length} rows${unit ? ` — ${unit}` : ""}`;

    if (rows.length === 0) {
      status.textContent = "No rows found.";
      return;
    }

    status.textContent = `Rendering 0 of ${rows.length} rows…`;

    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const tbody = document.createElement("tbody");
    const headerRow = document.createElement("tr");

    const columns = detail.historyType === "recorder"
      ? [
          ["Time", "last_updated_ts", "timestamp"],
          ["State", "state", "value"],
        ]
      : [
          ["Time", "start_ts", "timestamp"],
          ["Mean", "mean", "value"],
          ["Min", "min", "value"],
          ["Max", "max", "value"],
          ["State", "state", "value"],
          ["Sum", "sum", "value"],
          ["Last Reset", "last_reset_ts", "timestamp"],
        ];

    for (const [label] of columns) {
      const th = document.createElement("th");
      th.textContent = label;
      headerRow.appendChild(th);
    }

    thead.appendChild(headerRow);
    table.appendChild(thead);
    table.appendChild(tbody);
    tableWrap.appendChild(table);

    this._renderRowsInBatches(rows, columns, tbody, status, token, copyButton);
  }

  _renderRowsInBatches(rows, columns, tbody, status, token, copyButton) {
    const batchSize = 500;
    let index = 0;

    const renderBatch = () => {
      if (token !== this._renderToken) {
        return;
      }

      const fragment = document.createDocumentFragment();
      const end = Math.min(index + batchSize, rows.length);

      for (; index < end; index += 1) {
        const row = rows[index];
        const tr = document.createElement("tr");

        for (const [, key, type] of columns) {
          const td = document.createElement("td");
          const value = row?.[key];
          td.textContent = type === "timestamp"
            ? this._formatTimestamp(value)
            : value === null || value === undefined
              ? ""
              : String(value);
          tr.appendChild(td);
        }

        fragment.appendChild(tr);
      }

      tbody.appendChild(fragment);

      if (index < rows.length) {
        status.textContent = `Rendering ${index} of ${rows.length} rows…`;
        window.requestAnimationFrame(renderBatch);
      } else {
        status.textContent = "";
        status.hidden = true;
        copyButton.disabled = false;
      }
    };

    window.requestAnimationFrame(renderBatch);
  }

  _formatTimestamp(value) {
    if (value === null || value === undefined || value === "") {
      return "";
    }

    const numeric = Number(value);
    if (!Number.isFinite(numeric)) {
      return String(value);
    }

    const date = new Date(numeric * 1000);
    const timeZone = this._hass?.config?.time_zone;

    try {
      const formatter = new Intl.DateTimeFormat("en-CA", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
        ...(timeZone ? { timeZone } : {}),
      });

      const parts = Object.fromEntries(
        formatter
          .formatToParts(date)
          .filter((part) => part.type !== "literal")
          .map((part) => [part.type, part.value]),
      );

      return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
    } catch (_error) {
      return date.toLocaleString();
    }
  }

  _title(historyType) {
    switch (historyType) {
      case "short_term":
        return "Short-Term Statistics";
      case "long_term":
        return "Long-Term Statistics";
      default:
        return "Recorder History";
    }
  }
}

if (!customElements.get("recorder-history-query-buttons-card")) {
  customElements.define("recorder-history-query-buttons-card", RecorderHistoryQueryButtonsCard);
}

if (!customElements.get("recorder-history-query-result-card")) {
  customElements.define("recorder-history-query-result-card", RecorderHistoryQueryResultCard);
}

window.customCards = window.customCards || [];

if (!window.customCards.some((card) => card.type === "recorder-history-query-buttons-card")) {
  window.customCards.push({
    type: "recorder-history-query-buttons-card",
    name: "Recorder History Query Buttons",
    description: "Runs Recorder and statistics queries and sends the returned rows directly to the result card.",
  });
}

if (!window.customCards.some((card) => card.type === "recorder-history-query-result-card")) {
  window.customCards.push({
    type: "recorder-history-query-result-card",
    name: "Recorder History Query Result",
    description: "Displays Recorder and statistics query responses without storing them in an entity attribute.",
  });
}

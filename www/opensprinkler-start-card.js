const SPRINKLER_START_CARD_VERSION = "1.0.2";
const SPRINKLER_START_CARD_CSS_URL = new URL("./opensprinkler-program-card.css", import.meta.url);
SPRINKLER_START_CARD_CSS_URL.searchParams.set("v", SPRINKLER_START_CARD_VERSION);
const SPRINKLER_START_CARD_CSS = SPRINKLER_START_CARD_CSS_URL.href;

const STATION_DURATION_STOPS = Object.freeze([
  0,
  ...Array.from({ length: 15 }, (_, index) => index + 1),
  ...Array.from({ length: 9 }, (_, index) => 20 + index * 5),
  ...Array.from({ length: 8 }, (_, index) => 75 + index * 15),
]);

class SprinklerStartCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });

    this._config = null;
    this._hass = null;
    this._definitions = [];
    this._original = null;
    this._draft = null;
    this._starting = false;
    this._startError = "";
  }

  setConfig(config) {
    if (!config || typeof config.program_prefix !== "string" || !config.program_prefix.trim()) {
      throw new Error("opensprinkler-start-card: program_prefix is required");
    }

    if (config.stations !== undefined && !Array.isArray(config.stations)) {
      throw new Error("opensprinkler-start-card: stations must be a list");
    }

    const stations = config.stations?.map((item) => {
      if (typeof item === "string") {
        const station = item.trim();
        if (!station) {
          throw new Error("opensprinkler-start-card: every station must have a non-empty station name");
        }
        return { station };
      }

      if (!item || typeof item !== "object" || typeof item.station !== "string" || !item.station.trim()) {
        throw new Error(
          "opensprinkler-start-card: every station must be a string or an object with a non-empty station property",
        );
      }

      if (item.name !== undefined && (typeof item.name !== "string" || !item.name.trim())) {
        throw new Error("opensprinkler-start-card: station name must be a non-empty string when specified");
      }

      if (item.entity_id !== undefined && (typeof item.entity_id !== "string" || !item.entity_id.trim())) {
        throw new Error("opensprinkler-start-card: station entity_id must be a non-empty string when specified");
      }

      return {
        station: item.station.trim(),
        ...(item.name !== undefined ? { name: item.name.trim() } : {}),
        ...(item.entity_id !== undefined ? { entity_id: item.entity_id.trim() } : {}),
      };
    });

    this._config = {
      ...config,
      program_prefix: config.program_prefix.trim(),
      stations,
    };

    this._original = null;
    this._draft = null;
    this._definitions = [];
    this._startError = "";

    this._syncFromHass(true);
  }

  set hass(hass) {
    this._hass = hass;

    if (!this._config || this._starting) return;

    // Keep local slider edits while the user is preparing a manual run.
    if (this._isDirty()) return;

    this._syncFromHass(false);
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    this._syncFromHass(false);
  }

  getCardSize() {
    return 2 + this._definitions.length;
  }

  _entity(domain, suffix) {
    return `${domain}.${this._config.program_prefix}_${suffix}`;
  }

  _escapeRegExp(value) {
    return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  _titleCase(value) {
    return value
      .split("_")
      .filter(Boolean)
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(" ");
  }

  _stations() {
    if (Array.isArray(this._config.stations)) {
      return this._config.stations.map((item) => ({
        station: item.station,
        name: item.name ?? this._titleCase(item.station),
        runEntityId: item.entity_id,
      }));
    }

    if (!this._hass) return [];

    const prefix = this._escapeRegExp(this._config.program_prefix);
    const pattern = new RegExp(`^number\\.${prefix}_(.+)_station_duration$`);

    return Object.keys(this._hass.states)
      .map((entityId) => entityId.match(pattern)?.[1])
      .filter(Boolean)
      .sort((a, b) => this._titleCase(a).localeCompare(this._titleCase(b)))
      .map((station) => ({
        station,
        name: this._titleCase(station),
        runEntityId: undefined,
      }));
  }

  _buildDefinitions() {
    return this._stations().map(({ station, name, runEntityId }) => ({
      key: `station_${station}`,
      section: "stations",
      entityId: this._entity("number", `${station}_station_duration`),
      kind: "number",
      label: name,
      station,
      runEntityId,
    }));
  }

  _readValue(def) {
    const stateObj = this._hass?.states?.[def.entityId];
    if (!stateObj) return undefined;

    const value = Number(stateObj.state);
    return Number.isFinite(value) ? value : undefined;
  }

  _readSnapshot(definitions) {
    const snapshot = {};
    for (const def of definitions) {
      snapshot[def.key] = this._readValue(def);
    }
    return snapshot;
  }

  _metadataSignature(definitions) {
    return JSON.stringify(
      definitions.map((def) => {
        const stateObj = this._hass?.states?.[def.entityId];
        return {
          key: def.key,
          entityId: def.entityId,
          exists: Boolean(stateObj),
          min: stateObj?.attributes?.min,
          max: stateObj?.attributes?.max,
          step: stateObj?.attributes?.step,
          unit: stateObj?.attributes?.unit_of_measurement,
        };
      }),
    );
  }

  _syncFromHass(force) {
    if (!this._config || !this._hass || !this.isConnected) return;

    const definitions = this._buildDefinitions();
    const snapshot = this._readSnapshot(definitions);
    const metadataSignature = this._metadataSignature(definitions);
    const snapshotSignature = JSON.stringify(snapshot);

    const definitionsChanged =
      definitions.map((def) => def.entityId).join("|") !==
      this._definitions.map((def) => def.entityId).join("|");

    const shouldSync =
      force ||
      !this._draft ||
      definitionsChanged ||
      snapshotSignature !== this._snapshotSignature ||
      metadataSignature !== this._metadataSignatureValue;

    if (!shouldSync) return;

    this._definitions = definitions;
    this._original = { ...snapshot };
    this._draft = { ...snapshot };
    this._snapshotSignature = snapshotSignature;
    this._metadataSignatureValue = metadataSignature;
    this._startError = "";
    this._render();
  }

  _valuesEqual(a, b) {
    return Number(a) === Number(b);
  }

  _isDirty() {
    if (!this._original || !this._draft) return false;

    return this._definitions.some(
      (def) => !this._valuesEqual(this._original[def.key], this._draft[def.key]),
    );
  }

  _selectedStationDuration(def) {
    if (!this._draft) return 0;
    const stopIndex = this._stationDurationStopIndex(this._draft[def.key]);
    return STATION_DURATION_STOPS[stopIndex] ?? 0;
  }

  _hasActiveStations() {
    return this._definitions.some((def) => this._selectedStationDuration(def) > 0);
  }

  _escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  _stationDurationStopIndex(value) {
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) return 0;

    const exactIndex = STATION_DURATION_STOPS.indexOf(numericValue);
    if (exactIndex !== -1) return exactIndex;

    return STATION_DURATION_STOPS.reduce((bestIndex, stop, index) =>
      Math.abs(stop - numericValue) < Math.abs(STATION_DURATION_STOPS[bestIndex] - numericValue)
        ? index
        : bestIndex, 0);
  }

  _formatStationDuration(value) {
    const minutes = Number(value);
    if (!Number.isFinite(minutes)) return "";
    if (minutes === 0) return "Off";
    if (minutes < 60) return `${minutes} min`;

    const hours = Math.floor(minutes / 60);
    const remainingMinutes = minutes % 60;
    return `${hours}h ${remainingMinutes}m`;
  }

  _renderControl(def) {
    const stateObj = this._hass?.states?.[def.entityId];
    if (!stateObj) {
      return `<span class="missing">Missing: ${this._escapeHtml(def.entityId)}</span>`;
    }

    const value = this._draft?.[def.key];
    const stopIndex = this._stationDurationStopIndex(value);
    const duration = STATION_DURATION_STOPS[stopIndex];
    const durationText = this._formatStationDuration(duration);

    return `
      <div class="station-duration-control">
        <input
          class="station-duration-slider"
          type="range"
          data-key="${this._escapeHtml(def.key)}"
          data-station-duration="true"
          min="0"
          max="${STATION_DURATION_STOPS.length - 1}"
          step="1"
          value="${stopIndex}"
          aria-label="${this._escapeHtml(def.label)} duration"
          aria-valuetext="${this._escapeHtml(durationText)}"
        >
        <span
          class="station-duration-value"
          data-station-duration-value="${this._escapeHtml(def.key)}"
        >${this._escapeHtml(durationText)}</span>
      </div>
    `;
  }

  _renderRow(def) {
    return `
      <div class="entity-row station-duration-row">
        <div class="entity-name">${this._escapeHtml(def.label)}</div>
        <div class="entity-control">${this._renderControl(def)}</div>
      </div>
    `;
  }

  _render() {
    if (!this.shadowRoot || !this._config || !this._hass || !this._draft) return;

    const dirty = this._isDirty();
    const hasActiveStations = this._hasActiveStations();

    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="${SPRINKLER_START_CARD_CSS}">

      <ha-card>
        ${
          this._definitions.length
            ? this._definitions.map((def) => this._renderRow(def)).join("")
            : `<div class="empty-stations">No matching station-duration entities found.</div>`
        }

        <div class="footer">
          ${this._startError ? `<div class="save-error">${this._escapeHtml(this._startError)}</div>` : `<div class="save-error"></div>`}
          <button class="action-button cancel" type="button" data-action="cancel" ${this._starting || (!dirty && !this._isInPopup()) ? "disabled" : ""}>Cancel</button>
          <button class="action-button save" type="button" data-action="start" ${!hasActiveStations || this._starting ? "disabled" : ""}>${this._starting ? "Starting…" : "Start"}</button>
        </div>
      </ha-card>
    `;

    this.shadowRoot.querySelectorAll("[data-station-duration]").forEach((element) => {
      element.addEventListener("change", (event) => this._handleChange(event));
      element.addEventListener("input", (event) => this._handleChange(event));
    });

    this.shadowRoot
      .querySelector('[data-action="cancel"]')
      ?.addEventListener("click", () => this._cancel());

    this.shadowRoot
      .querySelector('[data-action="start"]')
      ?.addEventListener("click", () => this._start());
  }

  _handleChange(event) {
    const element = event.currentTarget;
    const key = element.dataset.key;
    if (!key || !this._draft) return;

    const def = this._definitions.find((item) => item.key === key);
    if (!def) return;

    const stopIndex = element.valueAsNumber;
    const value = STATION_DURATION_STOPS[stopIndex];
    if (value === undefined) return;

    this._draft[key] = value;
    this._startError = "";

    const durationText = this._formatStationDuration(value);
    element.setAttribute("aria-valuetext", durationText);

    const valueElement = this.shadowRoot?.querySelector(
      `[data-station-duration-value="${key}"]`,
    );
    if (valueElement) valueElement.textContent = durationText;

    this._updateActionButtons();
  }

  _updateActionButtons() {
    const dirty = this._isDirty();
    const hasActiveStations = this._hasActiveStations();
    const cancel = this.shadowRoot?.querySelector('[data-action="cancel"]');
    const start = this.shadowRoot?.querySelector('[data-action="start"]');

    if (cancel) cancel.disabled = this._starting || (!dirty && !this._isInPopup());
    if (start) start.disabled = !hasActiveStations || this._starting;
  }

  _isInPopup() {
    return Boolean(this.closest(".popup-card-overlay"));
  }

  _closePopup() {
    this.dispatchEvent(
      new CustomEvent("ll-custom", {
        detail: {
          browser_mod: {
            service: "browser_mod.close_popup",
          },
        },
        bubbles: true,
        composed: true,
      }),
    );

    this
      .closest(".popup-card-overlay")
      ?.querySelector(".popup-card-close")
      ?.click();
  }

  _cancel() {
    if (this._starting) return;

    if (this._isDirty()) {
      this._syncFromHass(true);
    }

    this._closePopup();
  }

  _commonPrefixScore(entityId) {
    const objectId = entityId.split(".", 2)[1] ?? "";
    const entityParts = objectId.split("_");
    const prefixParts = this._config.program_prefix.split("_");

    let score = 0;
    while (
      score < entityParts.length &&
      score < prefixParts.length &&
      entityParts[score] === prefixParts[score]
    ) {
      score += 1;
    }
    return score;
  }

  _resolveStationRunEntity(def) {
    if (def.runEntityId) {
      if (!this._hass.states[def.runEntityId]) {
        throw new Error(`${def.label}: missing ${def.runEntityId}`);
      }
      return def.runEntityId;
    }

    const exact = `switch.${def.station}_station_enabled`;
    if (this._hass.states[exact]) return exact;

    const suffix = `_${def.station}_station_enabled`;
    const matches = Object.keys(this._hass.states).filter(
      (entityId) => entityId.startsWith("switch.") && entityId.endsWith(suffix),
    );

    if (matches.length === 1) return matches[0];

    if (matches.length > 1) {
      const ranked = matches
        .map((entityId) => ({ entityId, score: this._commonPrefixScore(entityId) }))
        .sort((a, b) => b.score - a.score || a.entityId.localeCompare(b.entityId));

      if (ranked[0].score > ranked[1].score) return ranked[0].entityId;

      throw new Error(
        `${def.label}: multiple station switches match; specify entity_id in the station config`,
      );
    }

    throw new Error(
      `${def.label}: no switch.*_${def.station}_station_enabled entity found; specify entity_id in the station config`,
    );
  }

  async _start() {
    if (!this._hass || this._starting || !this._draft) return;

    const activeStations = this._definitions
      .map((def) => ({ def, minutes: this._selectedStationDuration(def) }))
      .filter(({ minutes }) => minutes > 0);

    if (activeStations.length === 0) return;

    let runs;
    try {
      // Resolve every station first so a bad mapping cannot produce a partial run.
      runs = activeStations.map(({ def, minutes }) => ({
        def,
        entityId: this._resolveStationRunEntity(def),
        runSeconds: Math.round(minutes * 60),
      }));
    } catch (error) {
      this._startError = error?.message ?? String(error);
      this._render();
      return;
    }

    this._starting = true;
    this._startError = "";
    this._render();

    try {
      // Queue stations in the same order as they appear on the card.
      for (const run of runs) {
        await this._hass.callService(
          "opensprinkler",
          "run_station",
          {
            run_seconds: run.runSeconds,
            queue_option: "append",
          },
          {
            entity_id: run.entityId,
          },
        );
      }

      this._starting = false;
      this._closePopup();
    } catch (error) {
      this._starting = false;
      this._startError = error?.message ?? String(error);
      this._render();
    }
  }
}

if (!customElements.get("opensprinkler-start-card")) {
  customElements.define("opensprinkler-start-card", SprinklerStartCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "opensprinkler-start-card")) {
  window.customCards.push({
    type: "opensprinkler-start-card",
    name: "Sprinkler Start Card",
    description: `Set temporary station runtimes and start the non-zero stations. v${SPRINKLER_START_CARD_VERSION}`,
    preview: false,
  });
}

console.info(`opensprinkler-start-card v${SPRINKLER_START_CARD_VERSION} loaded`);

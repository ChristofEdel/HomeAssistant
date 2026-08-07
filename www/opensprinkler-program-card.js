const OPENSPRINKLER_PROGRAM_CARD_VERSION = "1.5.1";
const OPENSPRINKLER_PROGRAM_CARD_CSS_URL = new URL("./opensprinkler-program-card.css", import.meta.url);
OPENSPRINKLER_PROGRAM_CARD_CSS_URL.searchParams.set("v", OPENSPRINKLER_PROGRAM_CARD_VERSION);
const OPENSPRINKLER_PROGRAM_CARD_CSS = OPENSPRINKLER_PROGRAM_CARD_CSS_URL.href;

class OpenSprinklerProgramCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });

    this._config = null;
    this._hass = null;
    this._definitions = [];
    this._original = null;
    this._draft = null;
    this._saving = false;
    this._saveError = "";
    this._pendingSave = new Map();
  }

  setConfig(config) {
    if (!config || typeof config.program_prefix !== "string" || !config.program_prefix.trim()) {
      throw new Error("opensprinkler-program-card: program_prefix is required");
    }

    if (config.stations !== undefined && !Array.isArray(config.stations)) {
      throw new Error("opensprinkler-program-card: stations must be a list");
    }

    const stations = config.stations?.map((item) => {
      if (typeof item === "string") {
        const station = item.trim();
        if (!station) {
          throw new Error("opensprinkler-program-card: every station must have a non-empty station name");
        }
        return { station };
      }

      if (!item || typeof item !== "object" || typeof item.station !== "string" || !item.station.trim()) {
        throw new Error(
          "opensprinkler-program-card: every station must be a string or an object with a non-empty station property",
        );
      }

      if (item.name !== undefined && (typeof item.name !== "string" || !item.name.trim())) {
        throw new Error("opensprinkler-program-card: station name must be a non-empty string when specified");
      }

      return {
        station: item.station.trim(),
        ...(item.name !== undefined ? { name: item.name.trim() } : {}),
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
    this._pendingSave.clear();
    this._saveError = "";

    this._syncFromHass(true);
  }

  set hass(hass) {
    this._hass = hass;

    if (!this._config) return;

    // After Save, keep showing the committed draft until all successful
    // service calls are reflected in hass.states. This avoids transient
    // reversion while state updates arrive one by one.
    if (this._pendingSave.size > 0) {
      let allApplied = true;
      for (const [key, expected] of this._pendingSave.entries()) {
        const def = this._definitions.find((item) => item.key === key);
        if (!def) continue;
        const actual = this._readValue(def);
        if (!this._valuesEqual(actual, expected)) {
          allApplied = false;
          break;
        }
      }

      if (allApplied) {
        this._pendingSave.clear();
        if (!this._isDirty()) this._syncFromHass(true);
      }
      return;
    }

    // Do not overwrite local edits with state refreshes while the form is dirty.
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
    const stationCount = this._definitions.filter((def) => def.section === "stations").length;
    return 8 + stationCount;
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
      }));
  }

  _buildDefinitions() {
    const p = (domain, suffix) => this._entity(domain, suffix);

    const defs = [
      {
        key: "enabled",
        section: "programme",
        entityId: p("switch", "program_enabled"),
        kind: "switch",
        label: "Enabled",
      },
      {
        key: "type",
        section: "programme",
        entityId: p("select", "type"),
        kind: "select",
        label: "Schedule type",
      },
      {
        key: "day_of_month",
        section: "programme",
        entityId: p("number", "day_of_month"),
        kind: "number",
        label: "Day of month",
        visibleWhen: (draft) => draft?.type === "Monthly",
      },
      {
        key: "interval_days",
        section: "programme",
        entityId: p("number", "interval_days"),
        kind: "number",
        label: "Repeat every",
        visibleWhen: (draft) => draft?.type === "Interval",
      },
      {
        key: "starting_in_days",
        section: "programme",
        entityId: p("number", "starting_in_days"),
        kind: "number",
        label: "Starting in",
        visibleWhen: (draft) => draft?.type === "Interval",
      },
      {
        key: "single_run_start_date",
        section: "programme",
        entityId: p("date", "single_run_start_date"),
        kind: "date",
        label: "Date",
        visibleWhen: (draft) => draft?.type === "Single-run",
      },
      ...[
        ["monday", "Mo"],
        ["tuesday", "Tu"],
        ["wednesday", "We"],
        ["thursday", "Th"],
        ["friday", "Fr"],
        ["saturday", "Sa"],
        ["sunday", "Su"],
      ].map(([day, shortLabel]) => ({
        key: `weekday_${day}`,
        section: "weekdays",
        entityId: p("switch", `${day}_enabled`),
        kind: "weekday",
        label: shortLabel,
        visibleWhen: (draft) => draft?.type === "Weekly",
      })),
      {
        key: "restrictions",
        section: "programme",
        entityId: p("select", "restrictions"),
        kind: "select",
        label: "Odd / Even / All",
      },
      {
        key: "start_time",
        section: "programme",
        entityId: p("time", "start_time"),
        kind: "time",
        label: "Start time",
      },
      ...this._stations().map(({ station, name }) => ({
        key: `station_${station}`,
        section: "stations",
        entityId: p("number", `${station}_station_duration`),
        kind: "number",
        label: name,
        station,
      })),
      {
        key: "use_weather",
        section: "stations",
        entityId: p("switch", "program_use_weather"),
        kind: "switch",
        label: "Weather adjustment",
        afterStations: true,
      },
    ];

    return defs;
  }

  _readValue(def) {
    const stateObj = this._hass?.states?.[def.entityId];
    if (!stateObj) return undefined;

    switch (def.kind) {
      case "switch":
      case "weekday":
        return stateObj.state === "on";

      case "number": {
        const value = Number(stateObj.state);
        return Number.isFinite(value) ? value : undefined;
      }

      case "select":
      case "date":
        return stateObj.state;

      case "time":
        // Keep the editor minute-precision even though HA time entities
        // normally expose HH:MM:SS.
        return String(stateObj.state).slice(0, 5);

      default:
        return stateObj.state;
    }
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
          options: stateObj?.attributes?.options,
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
    this._saveError = "";
    this._render();
  }

  _valuesEqual(a, b) {
    if (typeof a === "number" || typeof b === "number") {
      return Number(a) === Number(b);
    }
    return a === b;
  }

  _isDirty() {
    if (!this._original || !this._draft) return false;

    return this._definitions.some(
      (def) => !this._valuesEqual(this._original[def.key], this._draft[def.key]),
    );
  }

  _isVisible(def) {
    return !def.visibleWhen || def.visibleWhen(this._draft);
  }

  _escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  _renderControl(def) {
    const stateObj = this._hass?.states?.[def.entityId];
    if (!stateObj) {
      return `<span class="missing">Missing: ${this._escapeHtml(def.entityId)}</span>`;
    }

    const value = this._draft?.[def.key];
    if (def.kind === "switch") {
      return `
        <label class="toggle">
          <input type="checkbox" data-key="${this._escapeHtml(def.key)}" ${value ? "checked" : ""}>
          <span class="toggle-track"><span class="toggle-thumb"></span></span>
        </label>
      `;
    }

    if (def.kind === "select") {
      const options = Array.isArray(stateObj.attributes.options) ? [...stateObj.attributes.options] : [];
      if (value !== undefined && !options.includes(value)) options.unshift(value);

      return `
        <select class="select-control" data-key="${this._escapeHtml(def.key)}">
          ${options
            .map(
              (option) =>
                `<option value="${this._escapeHtml(option)}" ${option === value ? "selected" : ""}>${this._escapeHtml(option)}</option>`,
            )
            .join("")}
        </select>
      `;
    }

    if (def.kind === "number") {
      let min = stateObj.attributes.min;
      let max = stateObj.attributes.max;
      let step = stateObj.attributes.step ?? "any";
      let unit = stateObj.attributes.unit_of_measurement ?? "";

      if (def.key === "interval_days" || def.key === "starting_in_days") {
        min = 1;
        max = 100;
        step = 1;
      }

      if (def.key === "day_of_month") {
        min = -1;
        max = 31;
        step = 1;
      }

      return `
        <div class="number-wrap">
          <input
            class="number-control"
            type="number"
            data-key="${this._escapeHtml(def.key)}"
            value="${this._escapeHtml(value)}"
            ${min !== undefined ? `min="${this._escapeHtml(min)}"` : ""}
            ${max !== undefined ? `max="${this._escapeHtml(max)}"` : ""}
            step="${this._escapeHtml(step)}"
           
          >
          ${unit ? `<span class="unit">${this._escapeHtml(unit)}</span>` : ""}
        </div>
      `;
    }

    if (def.kind === "date") {
      return `
        <input
          class="date-control"
          type="date"
          data-key="${this._escapeHtml(def.key)}"
          value="${this._escapeHtml(value)}"
         
        >
      `;
    }

    if (def.kind === "time") {
      return `
        <input
          class="time-control"
          type="time"
          step="60"
          data-key="${this._escapeHtml(def.key)}"
          value="${this._escapeHtml(value)}"
         
        >
      `;
    }

    return "";
  }

  _renderRow(def) {
    if (!this._isVisible(def)) return "";

    return `
      <div class="entity-row">
        <div class="entity-name">${this._escapeHtml(def.label)}</div>
        <div class="entity-control">${this._renderControl(def)}</div>
      </div>
    `;
  }

  _renderWeekdays() {
    if (this._draft?.type !== "Weekly") return "";

    const weekdayDefs = this._definitions.filter((def) => def.section === "weekdays");

    return `
      <div class="weekday-row">
        ${weekdayDefs
          .map((def) => {
            const active = Boolean(this._draft?.[def.key]);
            const exists = Boolean(this._hass?.states?.[def.entityId]);
            return `
              <button
                type="button"
                class="weekday-button ${active ? "active" : ""}"
                data-weekday-key="${this._escapeHtml(def.key)}"
                aria-pressed="${active ? "true" : "false"}"
                title="${exists ? "" : this._escapeHtml(`Missing: ${def.entityId}`)}"
              >${this._escapeHtml(def.label)}</button>
            `;
          })
          .join("")}
      </div>
    `;
  }

  _render() {
    if (!this.shadowRoot || !this._config || !this._hass || !this._draft) return;

    const programmeDefs = this._definitions.filter(
      (def) => def.section === "programme" && !def.afterStations,
    );
    const stationDefs = this._definitions.filter(
      (def) => def.section === "stations" && !def.afterStations,
    );
    const weatherDef = this._definitions.find((def) => def.afterStations);
    const dirty = this._isDirty();

    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="${OPENSPRINKLER_PROGRAM_CARD_CSS}">


      <ha-card>
        ${programmeDefs
          .map((def) => {
            if (def.key === "restrictions") {
              return `${this._renderWeekdays()}${this._renderRow(def)}`;
            }
            return this._renderRow(def);
          })
          .join("")}

        <div class="section-divider" role="separator"></div>
        ${
          stationDefs.length
            ? stationDefs.map((def) => this._renderRow(def)).join("")
            : `<div class="empty-stations">No matching station-duration entities found.</div>`
        }
        ${weatherDef ? this._renderRow(weatherDef) : ""}

        <div class="footer">
          ${this._saveError ? `<div class="save-error">${this._escapeHtml(this._saveError)}</div>` : `<div class="save-error"></div>`}
          <button class="action-button cancel" type="button" data-action="cancel" ${this._saving || (!dirty && !this._isInPopup()) ? "disabled" : ""}>Cancel</button>
          <button class="action-button save" type="button" data-action="save" ${!dirty || this._saving ? "disabled" : ""}>${this._saving ? "Saving…" : "Save"}</button>
        </div>
      </ha-card>
    `;

    this.shadowRoot.querySelectorAll("[data-key]").forEach((element) => {
      element.addEventListener("change", (event) => this._handleChange(event));
      if (element.matches('input[type="number"]')) {
        element.addEventListener("input", (event) => this._handleChange(event));
      }
    });

    this.shadowRoot.querySelectorAll("[data-weekday-key]").forEach((button) => {
      button.addEventListener("click", (event) => this._toggleWeekday(event));
    });

    this.shadowRoot
      .querySelector('[data-action="cancel"]')
      ?.addEventListener("click", () => this._cancel());

    this.shadowRoot
      .querySelector('[data-action="save"]')
      ?.addEventListener("click", () => this._save());
  }

  _handleChange(event) {
    const element = event.currentTarget;
    const key = element.dataset.key;
    if (!key || !this._draft) return;

    const def = this._definitions.find((item) => item.key === key);
    if (!def) return;

    let value;
    if (def.kind === "switch") {
      value = element.checked;
    } else if (def.kind === "number") {
      if (element.value === "") return;
      value = element.valueAsNumber;
      if (!Number.isFinite(value)) return;
    } else {
      value = element.value;
    }

    this._draft[key] = value;
    this._saveError = "";

    // Schedule type changes which rows are visible, so rebuild the form.
    if (key === "type") {
      this._render();
    } else {
      this._updateActionButtons();
    }
  }

  _toggleWeekday(event) {
    const key = event.currentTarget.dataset.weekdayKey;
    if (!key || !this._draft) return;

    this._draft[key] = !this._draft[key];
    this._saveError = "";
    this._render();
  }

  _updateActionButtons() {
    const dirty = this._isDirty();
    const cancel = this.shadowRoot?.querySelector('[data-action="cancel"]');
    const save = this.shadowRoot?.querySelector('[data-action="save"]');

    if (cancel) cancel.disabled = this._saving || (!dirty && !this._isInPopup());
    if (save) save.disabled = !dirty || this._saving;
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
    if (this._saving) return;

    if (this._isDirty()) {
      this._pendingSave.clear();
      this._syncFromHass(true);
    }

    this._closePopup();
  }

  _allStationDurationDefinitions() {
    if (!this._hass || !this._config) return [];

    const prefix = this._escapeRegExp(this._config.program_prefix);
    const pattern = new RegExp(`^number\\.${prefix}_(.+)_station_duration$`);

    return Object.keys(this._hass.states)
      .map((entityId) => {
        const match = entityId.match(pattern);
        if (!match) return null;
        return {
          key: `station_${match[1]}`,
          section: "stations",
          entityId,
          kind: "number",
          station: match[1],
        };
      })
      .filter(Boolean);
  }

  _stationsNotOnCard() {
    const onCard = new Set(
      this._definitions
        .filter((def) => def.section === "stations" && def.kind === "number" && def.station)
        .map((def) => def.entityId),
    );

    return this._allStationDurationDefinitions().filter((def) => !onCard.has(def.entityId));
  }

  async _writeValue(def, value) {
    const entity_id = def.entityId;

    switch (def.kind) {
      case "switch":
      case "weekday":
        return this._hass.callService("switch", value ? "turn_on" : "turn_off", { entity_id });

      case "select":
        return this._hass.callService("select", "select_option", {
          entity_id,
          option: value,
        });

      case "number":
        return this._hass.callService("number", "set_value", {
          entity_id,
          value,
        });

      case "date":
        return this._hass.callService("date", "set_value", {
          entity_id,
          date: value,
        });

      case "time": {
        const time = /^\d{2}:\d{2}$/.test(String(value)) ? `${value}:00` : value;
        return this._hass.callService("time", "set_value", {
          entity_id,
          time,
        });
      }

      default:
        throw new Error(`Unsupported control type: ${def.kind}`);
    }
  }

  async _waitForValue(def, expected, timeoutMs = 10000) {
    const deadline = Date.now() + timeoutMs;

    while (Date.now() < deadline) {
      const actual = this._readValue(def);
      if (this._valuesEqual(actual, expected)) return;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }

    const actual = this._readValue(def);
    throw new Error(
      `${def.entityId} did not reach ${String(expected)} (current: ${String(actual)})`,
    );
  }

  _orderedWrites(changes, zeroedStations) {
    const changedByKey = new Map(changes.map((def) => [def.key, def]));
    const writes = [];
    const added = new Set();

    const add = (def, value, visibleChange = true) => {
      if (!def || added.has(def.entityId) || !this._hass.states[def.entityId]) return;
      writes.push({ def, value, visibleChange });
      added.add(def.entityId);
    };

    const addChanged = (key) => {
      const def = changedByKey.get(key);
      if (def) add(def, this._draft[key], true);
    };

    const typeChanged = changedByKey.has("type");
    const targetType = this._draft.type;

    // The schedule type must be confirmed before any fields whose meaning is
    // determined by that type are written.
    addChanged("type");

    if (targetType === "Monthly") {
      // Reassert the active schedule parameter after a type change even when
      // the value itself was not edited; changing type may rebuild the
      // controller-side schedule representation.
      const def = this._definitions.find((item) => item.key === "day_of_month");
      if (typeChanged) add(def, this._draft.day_of_month, true);
      else addChanged("day_of_month");
    } else if (targetType === "Interval") {
      for (const key of ["interval_days", "starting_in_days"]) {
        const def = this._definitions.find((item) => item.key === key);
        if (typeChanged) add(def, this._draft[key], true);
        else addChanged(key);
      }
    } else if (targetType === "Single-run") {
      const def = this._definitions.find((item) => item.key === "single_run_start_date");
      if (typeChanged) add(def, this._draft.single_run_start_date, true);
      else addChanged("single_run_start_date");
    } else if (targetType === "Weekly") {
      for (const def of changes.filter((item) => item.kind === "weekday")) {
        add(def, Boolean(this._draft[def.key]), true);
      }
    }

    // General programme settings follow the schedule-specific settings.
    addChanged("restrictions");
    addChanged("start_time");

    // Station cleanup and visible station edits operate on the same programme.
    // Reset omitted stations first, then apply the user's visible station
    // values so their explicit choices are the final station writes.
    for (const def of zeroedStations) add(def, 0, false);
    for (const def of changes.filter((item) => item.section === "stations" && item.kind === "number")) {
      add(def, this._draft[def.key], true);
    }

    addChanged("use_weather");

    // Enable/disable is deliberately last, after the programme definition has
    // been fully committed.
    addChanged("enabled");

    return writes;
  }

  async _save() {
    if (!this._hass || !this._isDirty() || this._saving) return;

    const changes = this._definitions.filter((def) => {
      if (!this._hass.states[def.entityId]) return false;
      return !this._valuesEqual(this._original[def.key], this._draft[def.key]);
    });

    // Explicit station lists represent the stations belonging to this program.
    // Any other matching station-duration entities are set to zero on Save.
    // Only issue a reset when the current value is non-zero.
    const zeroedStations = this._stationsNotOnCard().filter((def) => {
      const current = this._readValue(def);
      return current !== undefined && Number(current) !== 0;
    });

    const writes = this._orderedWrites(changes, zeroedStations);
    if (writes.length === 0) return;

    this._saving = true;
    this._saveError = "";
    this._pendingSave.clear();
    this._render();

    let failures = 0;
    let firstError = "";

    // Every programme write is transactional from the card's point of view:
    // issue one service call, wait until hass.states confirms that exact
    // requested value, and only then continue to the next programme write.
    for (const { def, value, visibleChange } of writes) {
      try {
        console.debug(
          `opensprinkler-program-card: saving ${def.entityId}`,
          value,
        );

        await this._writeValue(def, value);

        console.debug(
          `opensprinkler-program-card: waiting ${def.entityId}`,
          value,
        );
        await this._waitForValue(def, value);
        console.debug(
          `opensprinkler-program-card: saved ${def.entityId}`,
          value,
        );
        console.debug('--');
        if (visibleChange) {
          this._original[def.key] = value;
          this._pendingSave.set(def.key, value);
        }
      } catch (error) {
        failures += 1;
        if (!firstError) firstError = error?.message ?? String(error);

        console.error(
          `opensprinkler-program-card: failed to save ${def.entityId}`,
          error,
        );

        // Do not continue issuing dependent writes after an unconfirmed
        // programme update. That would recreate the race this sequencing is
        // intended to prevent.
        break;
      }
    }
    console.debug(`opensprinkler-program-card: saving complete`);

    this._saving = false;
    this._saveError = failures
      ? `Save stopped: ${firstError || "a programme change could not be confirmed."}`
      : "";

    if (failures === 0) {
      // All requested values have already been observed in hass.states, so a
      // fresh snapshot is safe immediately; no deferred pending-save state is
      // required.
      this._pendingSave.clear();
      this._syncFromHass(true);
      this._closePopup();
      return;
    }

    // Keep any unsaved draft values visible after a failure so the user can
    // retry. Successfully confirmed values have already been copied into
    // _original and therefore no longer count as dirty.
    this._pendingSave.clear();
    this._render();
  }

}

if (!customElements.get("opensprinkler-program-card")) {
  customElements.define("opensprinkler-program-card", OpenSprinklerProgramCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "opensprinkler-program-card")) {
  window.customCards.push({
    type: "opensprinkler-program-card",
    name: "OpenSprinkler Program Card",
    description: `Edit an OpenSprinkler program and commit changes with Save. v${OPENSPRINKLER_PROGRAM_CARD_VERSION}`,
    preview: false,
  });
}

console.info(`opensprinkler-program-card v${OPENSPRINKLER_PROGRAM_CARD_VERSION} loaded`);

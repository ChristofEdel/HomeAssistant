const ENERGY_DATE_RANGE_SELECTOR_CSS = new URL(
  "./energy-date-range-selector-card.css",
  import.meta.url
).href;

class EnergyDateRangeSelectorCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });

    this._dateRangePickerLoading = null;
    this._lastRangeKey = undefined;
    this._defaultRangeInitialized = false;
    this._pendingRange = null;

    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet";
    stylesheet.href = ENERGY_DATE_RANGE_SELECTOR_CSS;
    this.shadowRoot.appendChild(stylesheet);

    const card = document.createElement("ha-card");
    card.innerHTML = `
      <div class="card-content">
        <div class="content">
          <div class="date-range-selector">
            <div class="date-range-label"></div>
            <ha-date-range-picker
              id="date-range-picker"
              minimal
            ></ha-date-range-picker>
          </div>

          <div class="actions">
            <ha-button
              id="last-thirty-days"
              appearance="filled"
              size="s"
              variant="brand"
            >
              Last 30 days
            </ha-button>

            <ha-icon-button id="previous" label="Previous range">
              <ha-icon icon="mdi:chevron-left"></ha-icon>
            </ha-icon-button>

            <ha-icon-button id="next" label="Next range">
              <ha-icon icon="mdi:chevron-right"></ha-icon>
            </ha-icon-button>

          </div>
        </div>
      </div>
    `;

    this.shadowRoot.appendChild(card);

    this.shadowRoot
      .getElementById("last-thirty-days")
      .addEventListener("click", () => this._setDefaultRange());

    this.shadowRoot
      .getElementById("previous")
      .addEventListener("click", () => this._shiftRange(-1));

    this.shadowRoot
      .getElementById("next")
      .addEventListener("click", () => this._shiftRange(1));

    this.shadowRoot
      .getElementById("date-range-picker")
      .addEventListener("value-changed", (event) => {
        const startDate = event.detail?.value?.startDate;
        const endDate = event.detail?.value?.endDate;

        if (startDate instanceof Date && endDate instanceof Date) {
          this._setRange(
            this._toIsoDate(startDate),
            this._toIsoDate(endDate)
          );
        }
      });
  }

  _configureDateRangePickerOverlay() {
    const picker = this.shadowRoot.getElementById("date-range-picker");

    if (!picker?.shadowRoot) {
      return;
    }

    /*
     * Keep Home Assistant's native minimal picker as the real click target,
     * but make only its calendar button transparent.  The picker host itself
     * fills the visible date-range area, so HA gets a normal-sized #field to
     * anchor and size its popover from.
     */
    if (!picker.shadowRoot.getElementById("energy-range-overlay-style")) {
      const style = document.createElement("style");
      style.id = "energy-range-overlay-style";
      style.textContent = `
        :host {
          display: block;
          width: 100%;
          height: 100%;
        }

        .container,
        .date-range-inputs,
        #field {
          width: 100%;
          height: 100%;
        }

        #field {
          margin: 0;
          padding: 0;
          opacity: 0;
        }
      `;
      picker.shadowRoot.appendChild(style);
    }
  }

  setConfig(config) {
    if (!config?.start_entity || !config?.end_entity) {
      throw new Error(
        "energy-date-range-selector-card requires start_entity and end_entity"
      );
    }

    if (
      !config.start_entity.startsWith("input_datetime.") ||
      !config.end_entity.startsWith("input_datetime.")
    ) {
      throw new Error("start_entity and end_entity must be input_datetime entities");
    }

    this._config = config;
    this._ensureDateRangePicker();
    this._initializeDefaultRange();
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._ensureDateRangePicker();
    this._initializeDefaultRange();

    const range = this._currentRange();
    const currentRangeKey = range ? `${range.start}|${range.end}` : null;

    if (
      this._lastRangeKey !== undefined &&
      currentRangeKey &&
      currentRangeKey !== this._lastRangeKey
    ) {
      if (this._pendingRange) {
        if (
          range.start === this._pendingRange.start &&
          range.end === this._pendingRange.end
        ) {
          this._pendingRange = null;
          queueMicrotask(() => this._refreshLinkedApexCharts());
        }
      } else {
        queueMicrotask(() => this._refreshLinkedApexCharts());
      }
    }

    this._lastRangeKey = currentRangeKey;
    this._update();
  }

  getGridOptions() {
    return this._config?.grid_options ?? {};
  }

  async _initializeDefaultRange() {
    if (
      this._defaultRangeInitialized ||
      !this._hass ||
      !this._config?.start_entity ||
      !this._config?.end_entity
    ) {
      return;
    }

    this._defaultRangeInitialized = true;

    try {
      await this._setDefaultRange();
    } catch (error) {
      this._defaultRangeInitialized = false;
      console.error(
        "energy-date-range-selector-card: unable to initialize default range",
        error
      );
    }
  }

  async _ensureDateRangePicker() {
    if (customElements.get("ha-date-range-picker")) {
      this._configureDateRangePickerOverlay();
      this._update();
      return;
    }

    if (!this._config || !window.loadCardHelpers) {
      return;
    }

    if (!this._dateRangePickerLoading) {
      this._dateRangePickerLoading = (async () => {
        const helpers = await window.loadCardHelpers();

        /*
         * The standard energy date-selection card imports the native
         * ha-date-range-picker. Creating it is enough to load its module;
         * the temporary card is never added to the DOM.
         */
        const loader = await helpers.createCardElement({
          type: "energy-date-selection",
        });

        if (this._hass) {
          loader.hass = this._hass;
        }

        await customElements.whenDefined("ha-date-range-picker");
      })().finally(() => {
        this._dateRangePickerLoading = null;
      });
    }

    try {
      await this._dateRangePickerLoading;
      this._configureDateRangePickerOverlay();
      this._update();
    } catch (error) {
      console.error(
        "energy-date-range-selector-card: unable to load ha-date-range-picker",
        error
      );
    }
  }

  _update() {
    if (!this._hass || !this._config) {
      return;
    }

    const label = this.shadowRoot.querySelector(".date-range-label");
    const picker = this.shadowRoot.getElementById("date-range-picker");
    const previousButton = this.shadowRoot.getElementById("previous");
    const nextButton = this.shadowRoot.getElementById("next");
    const resetButton = this.shadowRoot.getElementById("last-thirty-days");

    label.classList.remove("error");

    const startEntity = this._hass.states[this._config.start_entity];
    const endEntity = this._hass.states[this._config.end_entity];

    if (!startEntity || !endEntity) {
      const missing = [
        !startEntity ? this._config.start_entity : null,
        !endEntity ? this._config.end_entity : null,
      ].filter(Boolean);

      label.textContent = `${missing.join(", ")} — entity not found`;
      label.classList.add("error");
      previousButton.disabled = true;
      nextButton.disabled = true;
      resetButton.disabled = true;
      picker.disabled = true;
      return;
    }

    const range = this._currentRange();

    if (!range) {
      label.textContent = "Date range entities have no valid date";
      label.classList.add("error");
      previousButton.disabled = true;
      nextButton.disabled = true;
      resetButton.disabled = true;
      picker.disabled = true;
      return;
    }

    label.textContent = this._formatRange(range.start, range.end);
    previousButton.disabled = false;
    nextButton.disabled = range.end >= this._yesterdayIso();
    resetButton.disabled = false;
    if (customElements.get("ha-date-range-picker")) {
      picker.hass = this._hass;
      picker.startDate = this._parseLocalDate(range.start);
      picker.endDate = this._parseLocalDate(range.end);
      picker.ranges = undefined;
      picker.disabled = false;
      this._configureDateRangePickerOverlay();
    }
  }

  _currentRange() {
    if (!this._hass || !this._config) {
      return null;
    }

    const startEntity = this._hass.states[this._config.start_entity];
    const endEntity = this._hass.states[this._config.end_entity];
    const start = startEntity ? this._entityDate(startEntity) : null;
    const end = endEntity ? this._entityDate(endEntity) : null;

    if (!start || !end) {
      return null;
    }

    return start <= end
      ? { start, end }
      : { start: end, end: start };
  }

  _entityDate(entity) {
    const state = entity.state;

    if (/^\d{4}-\d{2}-\d{2}$/.test(state)) {
      return state;
    }

    return entity.attributes?.date ?? null;
  }

  _formatRange(startIso, endIso) {
    const start = this._parseLocalDate(startIso);
    const end = this._parseLocalDate(endIso);
    const locale =
      this._hass?.locale?.language ||
      this._hass?.language ||
      navigator.language;

    const currentYear = new Date().getFullYear();
    const sameYear = start.getFullYear() === end.getFullYear();
    const sameMonth =
      sameYear && start.getMonth() === end.getMonth();

    if (sameMonth) {
      const month = new Intl.DateTimeFormat(locale, {
        month: "short",
      }).format(start);
      const year =
        start.getFullYear() !== currentYear
          ? ` ${start.getFullYear()}`
          : "";
      return `${start.getDate()}–${end.getDate()} ${month}${year}`;
    }

    const startOptions = {
      day: "numeric",
      month: "short",
    };
    const endOptions = {
      day: "numeric",
      month: "short",
    };

    if (!sameYear || start.getFullYear() !== currentYear) {
      startOptions.year = "numeric";
    }

    if (!sameYear || end.getFullYear() !== currentYear) {
      endOptions.year = "numeric";
    }

    const formatterStart = new Intl.DateTimeFormat(locale, startOptions);
    const formatterEnd = new Intl.DateTimeFormat(locale, endOptions);
    return `${formatterStart.format(start)} – ${formatterEnd.format(end)}`;
  }

  _parseLocalDate(iso) {
    const [year, month, day] = iso.split("-").map(Number);
    return new Date(year, month - 1, day);
  }

  _toIsoDate(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  _addDays(iso, days) {
    const date = this._parseLocalDate(iso);
    date.setDate(date.getDate() + days);
    return this._toIsoDate(date);
  }

  _daysInclusive(startIso, endIso) {
    const start = this._parseLocalDate(startIso);
    const end = this._parseLocalDate(endIso);
    const startUtc = Date.UTC(
      start.getFullYear(),
      start.getMonth(),
      start.getDate()
    );
    const endUtc = Date.UTC(
      end.getFullYear(),
      end.getMonth(),
      end.getDate()
    );

    return Math.round((endUtc - startUtc) / 86400000) + 1;
  }

  _todayIso() {
    return this._toIsoDate(new Date());
  }

  _yesterdayIso() {
    return this._addDays(this._todayIso(), -1);
  }

  async _setDefaultRange() {
    const end = this._yesterdayIso();
    const start = this._addDays(end, -29); /* 30 days inclusive */
    await this._setRange(start, end);
  }

  async _shiftRange(direction) {
    const range = this._currentRange();

    if (!range) {
      return;
    }

    const days = this._daysInclusive(range.start, range.end);
    let start = this._addDays(range.start, direction * days);
    let end = this._addDays(range.end, direction * days);
    const yesterday = this._yesterdayIso();

    if (end > yesterday) {
      end = yesterday;
      start = this._addDays(end, -(days - 1));
    }

    await this._setRange(start, end);
  }

  async _setRange(startIso, endIso) {
    if (!this._hass || !this._config) {
      return;
    }

    let start = startIso;
    let end = endIso;

    if (start > end) {
      [start, end] = [end, start];
    }

    const days = this._daysInclusive(start, end);
    const yesterday = this._yesterdayIso();

    if (end > yesterday) {
      end = yesterday;
      start = this._addDays(end, -(days - 1));
    }

    const current = this._currentRange();

    if (current?.start === start && current?.end === end) {
      this._refreshLinkedApexCharts();
      return;
    }

    this._pendingRange = { start, end };

    try {
      await Promise.all([
        this._setEntityDate(this._config.start_entity, start),
        this._setEntityDate(this._config.end_entity, end),
      ]);
    } catch (error) {
      this._pendingRange = null;
      throw error;
    }
  }

  async _setEntityDate(entityId, date) {
    const entity = this._hass.states[entityId];
    const current = entity ? this._entityDate(entity) : null;

    if (current === date) {
      return;
    }

    await this._hass.callService("input_datetime", "set_datetime", {
      entity_id: entityId,
      date,
    });
  }

  _refreshLinkedApexCharts() {
    if (!this._config?.start_entity || !this._config?.end_entity) {
      return;
    }

    const dateEntities = [
      this._config.start_entity,
      this._config.end_entity,
    ];
    const charts = this._findElementsInShadowDom("apexcharts-card");

    const linked = charts.filter((chart) => {
      try {
        const config = JSON.stringify(chart._config ?? {});
        return dateEntities.some((entityId) => config.includes(entityId));
      } catch (_) {
        return false;
      }
    });

    linked.forEach((chart) => {
      if (typeof chart._updateOnInterval === "function") {
        chart._updateOnInterval();
      }
    });

    if (!linked.length) {
      console.warn(
        `energy-date-range-selector-card: no apexcharts-card found using ${dateEntities.join(
          " or "
        )}`
      );
    }
  }

  _findElementsInShadowDom(tagName) {
    const found = [];
    const wanted = tagName.toLowerCase();

    const scan = (root) => {
      if (!root?.querySelectorAll) {
        return;
      }

      for (const element of root.querySelectorAll("*")) {
        if (element.localName === wanted) {
          found.push(element);
        }

        if (element.shadowRoot) {
          scan(element.shadowRoot);
        }
      }
    };

    scan(document);
    return found;
  }
}

if (!customElements.get("energy-date-range-selector-card")) {
  customElements.define(
    "energy-date-range-selector-card",
    EnergyDateRangeSelectorCard
  );
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "energy-date-range-selector-card",
  name: "Energy Date Range Selector Card",
  description: "Select an inclusive date range using two input_datetime helpers.",
});

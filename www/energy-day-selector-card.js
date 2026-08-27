const ENERGY_DAY_SELECTOR_CSS = new URL(
  "./energy-day-selector-card.css",
  import.meta.url
).href;

class EnergyDaySelectorCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });

    this._dateInputLoading = null;
    this._lastDate = undefined;
    this._todayInitialized = false;

    const stylesheet = document.createElement("link");
    stylesheet.rel = "stylesheet";
    stylesheet.href = ENERGY_DAY_SELECTOR_CSS;
    this.shadowRoot.appendChild(stylesheet);

    const card = document.createElement("ha-card");
    card.innerHTML = `
      <div class="card-content">
        <div class="content">
          <div class="date">
            <span class="date-label"></span>
            <ha-date-input id="date-picker"></ha-date-input>
          </div>

          <div class="actions">
            <ha-button
              id="today"
              appearance="filled"
              size="s"
              variant="brand"
            >
              Today
            </ha-button>

            <ha-icon-button id="previous" label="Previous">
              <ha-icon icon="mdi:chevron-left"></ha-icon>
            </ha-icon-button>

            <ha-icon-button id="next" label="Next">
              <ha-icon icon="mdi:chevron-right"></ha-icon>
            </ha-icon-button>
          </div>
        </div>
      </div>
    `;

    this.shadowRoot.appendChild(card);

    this.shadowRoot
      .getElementById("date-picker")
      .addEventListener("value-changed", (event) => {
        const value = event.detail?.value;
        if (value) {
          this._setDate(value);
        }
      });

    this.shadowRoot
      .getElementById("today")
      .addEventListener("click", () => this._setDate(this._todayIso()));

    this.shadowRoot
      .getElementById("previous")
      .addEventListener("click", () => this._shiftDate(-1));

    this.shadowRoot
      .getElementById("next")
      .addEventListener("click", () => this._shiftDate(1));
  }

  setConfig(config) {
    if (!config?.entity) {
      throw new Error("energy-day-selector-card requires an entity");
    }

    if (!config.entity.startsWith("input_datetime.")) {
      throw new Error("entity must be an input_datetime");
    }

    this._config = config;
    this._ensureDateInput();
    this._initializeToday();
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._ensureDateInput();
    this._initializeToday();

    const entity = this._config
      ? hass.states[this._config.entity]
      : null;

    const currentDate = entity ? this._entityDate(entity) : null;

    /*
     * Refresh linked ApexCharts whenever the configured input_datetime
     * actually changes, regardless of where that change originated.
     *
     * Do not refresh on the first hass assignment; only subsequent changes.
     */
    if (
      this._lastDate !== undefined &&
      currentDate &&
      currentDate !== this._lastDate
    ) {
      queueMicrotask(() => this._refreshLinkedApexCharts());
    }

    this._lastDate = currentDate;
    this._update();
  }

  getGridOptions() {
    return this._config?.grid_options ?? {};
  }

  async _initializeToday() {
    if (
      this._todayInitialized ||
      !this._hass ||
      !this._config?.entity
    ) {
      return;
    }

    this._todayInitialized = true;

    const entity = this._hass.states[this._config.entity];
    const current = entity ? this._entityDate(entity) : null;
    const today = this._todayIso();

    if (current === today) {
      return;
    }

    try {
      await this._hass.callService("input_datetime", "set_datetime", {
        entity_id: this._config.entity,
        date: today,
      });
    } catch (error) {
      this._todayInitialized = false;
      console.error(
        "energy-day-selector-card: unable to initialize date to today",
        error
      );
    }
  }

  async _ensureDateInput() {
    if (customElements.get("ha-date-input")) {
      this._update();
      return;
    }

    if (!this._config || !window.loadCardHelpers) {
      return;
    }

    if (!this._dateInputLoading) {
      this._dateInputLoading = (async () => {
        const helpers = await window.loadCardHelpers();

        /*
         * Creating the standard input_datetime row causes Home Assistant
         * to load its module. That module imports ha-date-input.
         * The row itself is never added to the DOM.
         */
        const row = await helpers.createRowElement({
          entity: this._config.entity,
        });

        if (this._hass) {
          row.hass = this._hass;
        }

        await customElements.whenDefined("ha-date-input");
      })().finally(() => {
        this._dateInputLoading = null;
      });
    }

    try {
      await this._dateInputLoading;
      this._update();
    } catch (error) {
      console.error(
        "energy-day-selector-card: unable to load ha-date-input",
        error
      );
    }
  }

  _update() {
    if (!this._hass || !this._config) {
      return;
    }

    const entity = this._hass.states[this._config.entity];
    const dateLabel = this.shadowRoot.querySelector(".date-label");
    const datePicker = this.shadowRoot.getElementById("date-picker");
    const nextButton = this.shadowRoot.getElementById("next");

    dateLabel.classList.remove("error");

    if (!entity) {
      dateLabel.textContent = `${this._config.entity} — entity not found`;
      dateLabel.classList.add("error");
      nextButton.disabled = true;
      return;
    }

    const iso = this._entityDate(entity);

    if (!iso) {
      dateLabel.textContent = `${this._config.entity} — entity has no date`;
      dateLabel.classList.add("error");
      nextButton.disabled = true;
      return;
    }

    dateLabel.textContent = this._formatDate(this._parseLocalDate(iso));
    nextButton.disabled = iso >= this._todayIso();

    if (customElements.get("ha-date-input")) {
      datePicker.locale = this._hass.locale;
      datePicker.value = iso;
      datePicker.max = this._todayIso();
      datePicker.disabled = false;
    }
  }

  _entityDate(entity) {
    const state = entity.state;

    if (/^\d{4}-\d{2}-\d{2}$/.test(state)) {
      return state;
    }

    return entity.attributes?.date ?? null;
  }

  _formatDate(date) {
    const locale =
      this._hass?.locale?.language ||
      this._hass?.language ||
      navigator.language;

    const options = {
      day: "numeric",
      month: "short",
    };

    if (date.getFullYear() !== new Date().getFullYear()) {
      options.year = "numeric";
    }

    return new Intl.DateTimeFormat(locale, options).format(date);
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

  _todayIso() {
    return this._toIsoDate(new Date());
  }

  async _shiftDate(days) {
    const entity = this._hass?.states[this._config?.entity];
    const iso = entity ? this._entityDate(entity) : null;

    if (!iso) {
      return;
    }

    const date = this._parseLocalDate(iso);
    date.setDate(date.getDate() + days);

    let target = this._toIsoDate(date);
    const today = this._todayIso();

    if (target > today) {
      target = today;
    }

    await this._setDate(target);
  }

  async _setDate(iso) {
    if (!this._hass || !this._config?.entity) {
      return;
    }

    const today = this._todayIso();
    const target = iso > today ? today : iso;
    const entity = this._hass.states[this._config.entity];
    const current = entity ? this._entityDate(entity) : null;

    /*
     * Re-selecting the already active day causes no HA state change,
     * so refresh explicitly in that one case.
     */
    if (target === current) {
      this._refreshLinkedApexCharts();
      return;
    }

    await this._hass.callService("input_datetime", "set_datetime", {
      entity_id: this._config.entity,
      date: target,
    });
  }

  _refreshLinkedApexCharts() {
    if (!this._config?.entity) {
      return;
    }

    const dateEntity = this._config.entity;
    const charts = this._findElementsInShadowDom("apexcharts-card");

    /*
     * Only refresh ApexCharts cards whose resolved configuration contains
     * this selector's input_datetime entity. In the energy graph template,
     * [[date]] is expanded to that entity inside the data_generator.
     */
    const linked = charts.filter((chart) => {
      try {
        return JSON.stringify(chart._config ?? {}).includes(dateEntity);
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
        `energy-day-selector-card: no apexcharts-card found using ${dateEntity}`
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

if (!customElements.get("energy-day-selector-card")) {
  customElements.define("energy-day-selector-card", EnergyDaySelectorCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "energy-day-selector-card",
  name: "Energy Day Selector Card",
  description: "Select a day for an input_datetime helper.",
});

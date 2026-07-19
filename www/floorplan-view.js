class FloorplanView extends HTMLElement {
  setConfig(config) {
    this.config = config;
    this.render();
  }

  set hass(hass) {
    this._hass = hass;
    this.render();
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    this.render();
  }

  toggleEntity(entityId) {
    if (!this.hass) return;

    this.hass.callService("homeassistant", "toggle", {
      entity_id: entityId,
    });
  }

  render() {
    if (!this.config || !this.hass) return;

    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }

    const image = this.config.image || "/local/floorplan.svg";
    const minWidth = this.config?.min_width || "400px";
    const maxWidth = this.config?.max_width || "1600px";
    const showGrid = this.config?.show_grid === true;
    
    const elements = this.config.elements || [];
    const defaultIconSize = this.config?.icon_size;

    // html for the icons
    const iconHtml = elements
      .map((item, index) => {
        const stateObj = this.hass.states[item.entity];
        const isOn = stateObj?.state === "on";

        const icon =
          item.icon ||
          stateObj?.attributes?.icon ||
          "mdi:lightbulb";

        const size = item.size || defaultIconSize || "auto";

        return `
            <div
                class="entity-icon-wrap ${isOn ? "on" : "off"}"
                data-index="${index}"
                title="${item.entity}"
                style="
                    left: ${item.left};
                    top: ${item.top};
                    width: ${size};
                "
            >
                <ha-icon class="entity-icon" icon="${icon}"></ha-icon>
            </div>
            `;
      })
      .join("");



    // Generate the overall HTML for the floor plan
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="/local/floorplan-view.css?v=1" />
      <div class="page">
        <div class="floorplan-wrap" style="width: clamp(${minWidth}, 100%, ${maxWidth})">
          <img class="floorplan" src="${image}" />
          ${gridHtml}
          ${iconHtml}
        </div>
      </div>
    `;

    // Hook up events for touch and for hold
    this.shadowRoot.querySelectorAll(".entity-icon-wrap").forEach((el) => {
        const index = Number(el.dataset.index);
        const item = elements[index];
        const entityId = item.entity;

        let holdTimer = null;
        let held = false;

        const clearHoldTimer = () => {
            if (holdTimer !== null) {
                window.clearTimeout(holdTimer);
                holdTimer = null;
            }
        };

        const showMoreInfo = () => {
            this.dispatchEvent(
                new CustomEvent("hass-more-info", {
                    detail: { entityId },
                    bubbles: true,
                    composed: true,
                })
            );
        };

        el.addEventListener("pointerdown", (event) => {
            event.preventDefault();
            held = false;

            holdTimer = window.setTimeout(() => {
                held = true;

                const action = item.hold_action?.action || "more-info";

                if (action === "more-info") {
                    showMoreInfo();
                }
            }, 600);
        });

        el.addEventListener("pointerup", (event) => {
            event.preventDefault();
            clearHoldTimer();

            if (held) return;

            const action = item.tap_action?.action || "toggle";

            if (action === "toggle") {
                this.toggleEntity(entityId);
            }

            if (action === "more-info") {
                showMoreInfo();
            }
        });

        el.addEventListener("pointerleave", clearHoldTimer);
        el.addEventListener("pointercancel", clearHoldTimer);
    });
  }
}

customElements.define("floorplan-view", FloorplanView);
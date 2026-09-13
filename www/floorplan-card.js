class FloorplanCard extends HTMLElement {

    constructor() {
        super();

        // Create the shadow DOM for the card
        this.attachShadow({ mode: "open" });

        this._config = [];
        this._rendered = false;
        this._holdTimers = new Set();
    }

    // default configuration (for use by the card picker)
    static getStubConfig() {
        return {
            image: "/local/<floorplan>.svg",
            elements: [],
        };
    }

    // Apply the configuration
    setConfig(config) {

        // Check for presence of the main configuration parts
        if (!config) throw new Error("Floorplan card configuration is required");
        if (config.elements !== undefined && !Array.isArray(config.elements)) {
            throw new Error("'elements' must be an array");
        }

        for (const [index, item] of (config.elements || []).entries()) {
            if (!item.entity) {
                throw new Error(`Element ${index + 1} requires an entity`);
            }

            if (item.left === undefined || item.top === undefined) {
                throw new Error(
                    `Element ${index + 1} requires left and top positions`
                );
            }
        }

        // Store the configuration
        this._config = config;

        // Render if necessary
        this._rendered = false;
        this.clearHoldTimers();
        if (this.isConnected && this._hass) {
            this.render();
        }
    }

    set hass(hass) {
        this._hass = hass;

        if (!this._rendered) {
            this.render();
            // includes updateEntityStates()
        }
        else {
            this.updateEntityStates();
        }

    }

    get hass() {
        return this._hass;
    }

    connectedCallback() {
        this.render();
    }

    disconnectedCallback() {
        this.clearHoldTimers();
    }

    /*
     * Used by masonry views.
     * One unit is approximately 50 px.
     */
    getCardSize() {
        const size = Number(this._config?.card_size);

        return Number.isFinite(size) && size > 0
            ? size
            : 6;
    }

    /*
     * Used by Sections views.
     *
     * A section has 12 columns, so six columns means that
     * two floorplan cards can be placed next to each other.
     *
     * Rows are omitted so the SVG determines the card height.
     */
    getGridOptions() {
        const options = this._config?.grid_defaults ?? {};
        return {
            columns:     options.columns     ?? 6,
            rows:        options.rows,
            min_columns: options.min_columns ?? 3,
            max_columns: options.max_columns ?? 12,
            min_rows:    options.min_rows    ?? 1,
            max_rows:    options.max_rows
        };
    }

    toggleEntity(entityId) {
        if (!this._hass || !entityId) {
            return;
        }

        this._hass.callService("homeassistant", "toggle", {
            entity_id: entityId,
        });
    }

    showMoreInfo(entityId) {
        this.dispatchEvent(
            new CustomEvent("hass-more-info", {
                detail: { entityId },
                bubbles: true,
                composed: true,
            })
        );
    }

    runAction(item, actionType /* hold or tap */) {
        const defaultAction =
            actionType === "hold" ? "more-info" : "toggle";

        const actionConfig = item[`${actionType}_action`] || {
            action: defaultAction,
        };

        const action = actionConfig.action || defaultAction;

        switch (action) {
            case "toggle":
                this.toggleEntity(item.entity);
                break;

            case "more-info":
                this.showMoreInfo(item.entity);
                break;

            case "fire-dom-event":
                this.dispatchEvent(
                    new CustomEvent("ll-custom", {
                        detail: actionConfig,
                        bubbles: true,
                        composed: true,
                    })
                );
                break;


            case "none":
                break;

            default:
                console.warn(
                    `Unsupported floorplan action: ${action}`
                );
        }
    }

    render() {
        if (!this._config || !this._hass || !this.isConnected) {
            return;
        }

        this.clearHoldTimers();

        const image    = this._config.image || "/local/floorplan.svg";
        const minWidth = this._config.min_width || "100px";
        const maxWidth = this._config.max_width || "1600px";
        const showGrid = this._config.show_grid === true;
        const elements = this._config.elements || [];

        // HTML for all element icons
        const iconHtml = elements
            .map(
                (_, index) => `
                    <div
                        class="entity-icon-wrap"
                        data-index="${index}"
                        role="button"
                        tabindex="0"
                    >
                        <ha-icon class="entity-icon"></ha-icon>
                    </div>
                `
            )
            .join("");

        // HTML for the grid overlay (if enabled)
        const gridHtml = showGrid
            ? `
                <div class="position-grid">
                ${Array.from({ length: 11 }, (_, i) => {
                        const p = i * 10;
                        return `
                    <div class="grid-line vertical" style="left: ${p}%"></div>
                    <div class="grid-line horizontal" style="top: ${p}%"></div>
                    <div class="grid-label x" style="left: ${p}%">${p}%</div>
                    <div class="grid-label y" style="top: ${p}%">${p}%</div>
                    `;
                    }).join("")}
                </div>
            `
            : "";

        // HTML for the overall card
        this.shadowRoot.innerHTML = `
            <link rel="stylesheet" href="/local/floorplan-card.css?v=3" />
            <ha-card class="${this._config.transparent === true
                ? "transparent"
                : ""
            }">
            ${this._config.title
                ? '<div class="card-header"></div>'
                : ""
            }
                <div class="floorplan-wrap">
                    <img class="floorplan" alt="" draggable="false">
                    ${gridHtml}
                    ${iconHtml}
                </div>
            </ha-card>
        `;

        // Set width, and image URL and image source
        // We don't do this in the HTML generation above so we don't have to worry about
        // quotes and HTML in the configuration
        const floorplanWrap = this.shadowRoot.querySelector(".floorplan-wrap");
        floorplanWrap.style.width = `clamp(${minWidth}, 100%, ${maxWidth})`;

        const floorplanImage = this.shadowRoot.querySelector(".floorplan");
        floorplanImage.src = image;

        const header = this.shadowRoot.querySelector(".card-header");
        if (header) header.textContent = this._config.title;

        // Same for the position and size of each element
        elements.forEach((item, index) => {
            const htmlElement =
                this.shadowRoot.querySelector(
                    `[data-index="${index}"]`
                );

            htmlElement.style.left = item.left;
            htmlElement.style.top = item.top;
            htmlElement.style.width = item.size || this._config?.icon_size || "auto";

            this.attachActions(htmlElement, item);
        });

        this._rendered = true;
        this.updateEntityStates();
    }

    updateEntityStates() {
        if (!this._rendered || !this._hass) {
            return;
        }

        const elements =
            this._config.elements || [];

        elements.forEach((item, index) => {
            const htmlElement =
                this.shadowRoot.querySelector(
                    `[data-index="${index}"]`
                );

            if (!htmlElement) {
                return;
            }

            const isClimate = item.entity.startsWith("climate.");

            const stateObj =
                this._hass.states[item.entity];

            const state =
                stateObj?.state;

            const unavailable =
                !stateObj ||
                state === "unavailable" ||
                state === "unknown";

            const isOn =
                state === "on"
                || (isClimate && state !== "off");

            const icon =
                item.icon ||
                stateObj?.attributes?.icon ||
                "mdi:lightbulb";

            const name =
                stateObj?.attributes?.friendly_name ||
                item.entity;

            const stateText =
                state || "unavailable";

            htmlElement.classList.toggle(
                "on",
                isOn && !unavailable
            );

            htmlElement.classList.toggle(
                "off",
                !isOn && !unavailable
            );

            htmlElement.classList.toggle(
                "unavailable",
                unavailable
            );

            htmlElement.title =
                `${name}: ${stateText}`;

            htmlElement.setAttribute(
                "aria-label",
                `${name}: ${stateText}`
            );

            htmlElement
                .querySelector("ha-icon")
                .setAttribute("icon", icon);
        });
    }

    attachActions(htmlElement, item) {
        let holdTimer = null;
        let held = false;

        const clearHoldTimer = () => {
            if (holdTimer !== null) {
                window.clearTimeout(holdTimer);
                this._holdTimers.delete(holdTimer);
                holdTimer = null;
            }
        };

        htmlElement.addEventListener(
            "pointerdown",
            (event) => {
                event.preventDefault();
                held = false;

                holdTimer = window.setTimeout(() => {
                    held = true;

                    this._holdTimers.delete(holdTimer);
                    holdTimer = null;

                    this.runAction(item, "hold");
                }, 600);

                this._holdTimers.add(holdTimer);
            }
        );

        htmlElement.addEventListener(
            "pointerup",
            (event) => {
                event.preventDefault();
                clearHoldTimer();

                if (!held) {
                    this.runAction(item, "tap");
                }
            }
        );

        htmlElement.addEventListener(
            "pointerleave",
            clearHoldTimer
        );

        htmlElement.addEventListener(
            "pointercancel",
            clearHoldTimer
        );

        htmlElement.addEventListener(
            "contextmenu",
            (event) => event.preventDefault()
        );

        htmlElement.addEventListener(
            "keydown",
            (event) => {
                if (
                    event.key !== "Enter" &&
                    event.key !== " "
                ) {
                    return;
                }

                event.preventDefault();
                this.runAction(item, "tap");
            }
        );
    }

    clearHoldTimers() {
        for (const timer of this._holdTimers) {
            window.clearTimeout(timer);
        }

        this._holdTimers.clear();
    }
}

if (!customElements.get("floorplan-card")) {
    customElements.define(
        "floorplan-card",
        FloorplanCard
    );
}

window.customCards =
    window.customCards || [];

if (
    !window.customCards.some(
        (card) => card.type === "floorplan-card"
    )
) {
    window.customCards.push({
        type: "floorplan-card",
        name: "Floorplan Card",
        description:
            "An interactive SVG floorplan card",
        preview: false,
    });
}
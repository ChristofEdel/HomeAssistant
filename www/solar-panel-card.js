const SOLAR_PANEL_STYLESHEET_URL = new URL(
  "./solar-panel-card.css?v=1",
  import.meta.url
).href;

class SolarPanelCard extends HTMLElement {
  constructor() {
    super();

    this.attachShadow({ mode: "open" });

    this._config = undefined;
    this._hass = undefined;
    this._history = [];
    this._limitHistory = [];
    this._lastHistoryLoad = 0;
    this._lastState = undefined;
    this._lastLimitState = undefined;
    this._loadingHistory = false;
    this._error = undefined;

    this._buildDom();
  }

  _buildDom() {
    this._stylesheet = document.createElement("link");
    this._stylesheet.rel = "stylesheet";
    this._stylesheet.href = SOLAR_PANEL_STYLESHEET_URL;

    this._card = document.createElement("ha-card");
    this._card.className = "solar-panel-card";

    // Filled from the bottom according to the sensor percentage.
    this._levelFill = document.createElement("div");
    this._levelFill.className = "level-fill";
    this._levelFill.setAttribute("aria-hidden", "true");

    // All visible card content sits above the background fill.
    this._content = document.createElement("div");
    this._content.className = "content";

    this._value = document.createElement("div");
    this._value.className = "value";

    this._graph = document.createElement("div");
    this._graph.className = "graph";

    this._content.append(this._value, this._graph);
    this._card.append(this._levelFill, this._content);
    this.shadowRoot.append(this._stylesheet, this._card);

    this._card.addEventListener("click", () => {
      this._showMoreInfo();
    });
  }

  setConfig(config) {
    if (!config.entity) {
      throw new Error("entity is required");
    }

    const min = Number(config.min ?? 0);
    const max = Number(config.max ?? 100);
    const limitDivider = Number(config.limit_divider ?? 1);

    if (!Number.isFinite(min) || !Number.isFinite(max)) {
      throw new Error("min and max must be valid numbers");
    }

    if (max <= min) {
      throw new Error("max must be greater than min");
    }

    if (!Number.isFinite(limitDivider) || limitDivider <= 0) {
      throw new Error("limit_divider must be greater than zero");
    }

    this._config = {
      entity: config.entity,

      limit_entity: config.limit_entity,
      limit_divider: limitDivider,

      min: min,
      max: max,

      // Number of hours shown in the trend graph.
      hours_to_show: Number(config.hours_to_show ?? 24),

      // Granulatity of the trend graph data points in minutes. 0 = disabled (no downsampling).
      sample_interval: Number(config.sample_interval ?? 5),

      // Flag indicating whether the trend graph should be smoothed with quadratic Bézier curves.
      smooth: config.smooth === undefined ? true : Boolean(config.smooth),

      // Minimum number of minutes between history API requests.
      refresh_interval: Number(config.refresh_interval ?? 5),

      // Optional fixed number of decimal places.
      decimals:
        config.decimals === undefined
          ? 0
          : Number(config.decimals),

      // Overall card and graph dimensions.
      card_height: Number(config.card_height ?? 150),
      graph_height: Number(config.graph_height ?? 75),

      // Optional fixed vertical graph scale.
      graph_min:
        config.graph_min === undefined
          ? undefined
          : Number(config.graph_min),

      graph_max:
        config.graph_max === undefined
          ? undefined
          : Number(config.graph_max),

      // CSS colour values.
      fill_color:
        config.fill_color ?? "var(--primary-color)",

      fill_opacity: Number(config.fill_opacity ?? 0.4),

      graph_color:
        config.graph_color ?? "var(--state-icon-color)",
    };

    this._history = [];
    this._limitHistory  = [];
    this._lastHistoryLoad = 0;
    this._lastState = undefined;
    this._lastLimitState = undefined;
    this._error = undefined;

    this._applyConfigurationStyles();
    this._render();
    this._loadHistoryIfRequired(true);
  }

  set hass(hass) {
    this._hass = hass;

    if (!this._config) {
      return;
    }

    const stateObject = hass.states[this._config.entity];

    // Add a new local graph point immediately when the state changes.
    if (stateObject && stateObject.state !== this._lastState) {
      this._lastState = stateObject.state;

      let value = Number(stateObject.state);
      if (Number.isNaN(value)) value = 0;

      if (Number.isFinite(value)) {
        this._addHistoryPoint(Date.now(), value);
      }
    }

    const limitStateObject = this._config.limit_entity
      ? hass.states[this._config.limit_entity]
      : undefined;

    // Add a new local limit point immediately when its state changes.
    if (
      limitStateObject &&
      limitStateObject.state !== this._lastLimitState
    ) {
      this._lastLimitState = limitStateObject.state;

      const value = Number(limitStateObject.state);

      if (Number.isFinite(value)) {
        this._addLimitHistoryPoint(Date.now(), value);
      }
    }

    this._render();
    this._loadHistoryIfRequired(false);
  }

  connectedCallback() {
    this._render();
    this._loadHistoryIfRequired(false);
  }

  getCardSize() {
    if (!this._config) {
      return 3;
    }

    return Math.max(
      1,
      Math.ceil(this._config.card_height / 50)
    );
  }

  getGridOptions() {
    return {
      rows: 3,
      columns: 6,
      min_rows: 2,
      min_columns: 3,
    };
  }

  _applyConfigurationStyles() {
    if (!this._config) {
      return;
    }

    this._card.style.setProperty(
      "--solar-panel-card-height",
      `${this._config.card_height}px`
    );

    this._card.style.setProperty(
      "--solar-panel-graph-height",
      `${this._config.graph_height}px`
    );

    this._card.style.setProperty(
      "--solar-panel-fill-color",
      this._config.fill_color
    );

    this._card.style.setProperty(
      "--solar-panel-fill-opacity",
      String(
        Math.max(
          0,
          Math.min(1, this._config.fill_opacity)
        )
      )
    );

    this._card.style.setProperty(
      "--solar-panel-graph-color",
      this._config.graph_color
    );
  }

  async _loadHistoryIfRequired(force) {
    if (
      !this._hass ||
      !this._config ||
      this._loadingHistory ||
      !this.isConnected
    ) {
      return;
    }

    const refreshMilliseconds =
      Math.max(1, this._config.refresh_interval) *
      60 *
      1000;

    if (
      !force &&
      Date.now() - this._lastHistoryLoad <
      refreshMilliseconds
    ) {
      return;
    }

    this._loadingHistory = true;
    this._error = undefined;
    this._renderGraph();

    const end = new Date();

    const start = new Date(
      end.getTime() -
      Math.max(0.1, this._config.hours_to_show) *
      60 *
      60 *
      1000
    );

    try {
      const historyPromise = this._loadEntityHistory(
        this._config.entity,
        start,
        end
      );

      // A failure of the optional limit history must not hide
      // the main entity history.
      const limitHistoryPromise = this._config.limit_entity
        ? this._loadEntityHistory(
            this._config.limit_entity,
            start,
            end
          ).catch((error) => {
            console.error(
              "SolarPanelCard could not load limit history",
              error
            );

            return this._limitHistory;
          })
        : Promise.resolve([]);

      const [history, limitHistory] = await Promise.all([
        historyPromise,
        limitHistoryPromise,
      ]);

      this._history = history;
      this._limitHistory = limitHistory;
      this._lastHistoryLoad = Date.now();
    } catch (error) {
      console.error(
        "SolarPanelCard could not load history",
        error
      );

      this._error = "Unable to load history";
    } finally {
      this._loadingHistory = false;
      this._renderGraph();
    }
  }

  async _loadEntityHistory(entityId, start, end) {
    const path =
      `history/period/${encodeURIComponent(
        start.toISOString()
      )}` +
      `?filter_entity_id=${encodeURIComponent(
        entityId
      )}` +
      `&end_time=${encodeURIComponent(
        end.toISOString()
      )}` +
      "&minimal_response" +
      "&no_attributes";

    const response = await this._hass.callApi(
      "get",
      path
    );

    const states = Array.isArray(response?.[0])
      ? response[0]
      : [];

    const points = states
      .map((item) => ({
        timestamp: Date.parse(
          item.last_updated ??
          item.last_changed ??
          ""
        ),
        value: Number(item.state),
      }))
      .filter(
        (point) =>
          Number.isFinite(point.timestamp) &&
          Number.isFinite(point.value)
      );

    const currentState = this._hass.states[entityId];
    const currentValue = Number(currentState?.state);

    if (Number.isFinite(currentValue)) {
      points.push({
        timestamp: Date.now(),
        value: currentValue,
      });
    }

    return this._normalisePoints(points);
  }

  _addHistoryPoint(timestamp, value) {
    this._history.push({
      timestamp,
      value,
    });

    this._history = this._normalisePoints(
      this._history
    );
  }

  _addLimitHistoryPoint(timestamp, value) {
    this._limitHistory.push({
      timestamp,
      value,
    });

    this._limitHistory = this._normalisePoints(
      this._limitHistory
    );
  }

  _normalisePoints(points) {
    if (!this._config) {
      return [];
    }

    const cutoff =
      Date.now() -
      this._config.hours_to_show *
      60 *
      60 *
      1000;

    const pointsByTimestamp = new Map();

    for (const point of points) {
      if (
        point.timestamp >= cutoff &&
        Number.isFinite(point.value)
      ) {
        pointsByTimestamp.set(
          point.timestamp,
          point
        );
      }
    }

    return [...pointsByTimestamp.values()].sort(
      (left, right) =>
        left.timestamp - right.timestamp
    );
  }

  _downsamplePoints(points) {
    const intervalMinutes = Math.max(
      0,
      this._config.sample_interval
    );

    if (intervalMinutes === 0 || points.length < 2) {
      return points;
    }

    const intervalMilliseconds =
      intervalMinutes * 60 * 1000;

    const buckets = new Map();

    for (const point of points) {
      const bucketTimestamp =
        Math.floor(
          point.timestamp / intervalMilliseconds
        ) * intervalMilliseconds;

      let bucket = buckets.get(bucketTimestamp);

      if (!bucket) {
        bucket = {
          timestampTotal: 0,
          valueTotal: 0,
          count: 0,
        };

        buckets.set(bucketTimestamp, bucket);
      }

      bucket.timestampTotal += point.timestamp;
      bucket.valueTotal += point.value;
      bucket.count += 1;
    }

    return [...buckets.values()]
      .map((bucket) => ({
        timestamp:
          bucket.timestampTotal / bucket.count,

        value:
          bucket.valueTotal / bucket.count,
      }))
      .sort(
        (left, right) =>
          left.timestamp - right.timestamp
      );
  }

  _formatState(stateObject) {
    if (!stateObject) {
      return "Unavailable";
    }

    const numericValue = Number(stateObject.state);

    if (!Number.isFinite(numericValue)) {
      return stateObject.state;
    }

    // Let Home Assistant apply its standard state
    // formatting when no explicit precision was given.
    if (
      !Number.isInteger(this._config.decimals) &&
      this._hass?.formatEntityState
    ) {
      return this._hass.formatEntityState(
        stateObject
      );
    }

    const decimals = Number.isInteger(
      this._config.decimals
    )
      ? Math.max(0, this._config.decimals)
      : 0;

    const valueText = new Intl.NumberFormat(
      this._hass?.locale?.language,
      {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      }
    ).format(numericValue);

    const unit =
      stateObject.attributes
        .unit_of_measurement ?? "";

    return unit
      ? `${valueText} ${unit}`
      : valueText;
  }

  _calculatePercentage(value) {
    const percentage =
      ((value - this._config.min) /
        (this._config.max -
          this._config.min)) *
      100;

    return Math.max(
      0,
      Math.min(100, percentage)
    );
  }

  _createGraphPath() {
    const graphPoints =
      this._downsamplePoints(this._history);

    if (!graphPoints.length) {
      return "";
    }

    const now = Date.now();

    const start =
      now -
      this._config.hours_to_show *
      60 *
      60 *
      1000;

    const values = graphPoints.map(
      (point) => point.value
    );

    let graphMin = Number.isFinite(
      this._config.graph_min
    )
      ? this._config.graph_min
      : Math.min(...values);

    let graphMax = Number.isFinite(
      this._config.graph_max
    )
      ? this._config.graph_max
      : Math.max(...values);

    if (graphMax === graphMin) {
      graphMax = graphMin + 1;
    }

    const points = graphPoints.map(
      (point) => {
        const x =
          ((point.timestamp - start) /
            (now - start)) *
          100;

        const y =
          40 -
          ((point.value - graphMin) /
            (graphMax - graphMin)) *
          40;

        return {
          x: Math.max(
            0,
            Math.min(100, x)
          ),

          y: Math.max(
            0,
            Math.min(40, y)
          ),
        };
      }
    );

    if (points.length === 1) {
      return (
        `M 0 ${points[0].y.toFixed(2)} ` +
        `L 100 ${points[0].y.toFixed(2)}`
      );
    }

    if (this._config.smooth !== true) {
      return points
        .map((point, index) => {
          const command =
            index === 0 ? "M" : "L";

          return (
            `${command} ` +
            `${point.x.toFixed(2)} ` +
            `${point.y.toFixed(2)}`
          );
        })
        .join(" ");
    }

    let path =
      `M ${points[0].x.toFixed(2)} ` +
      `${points[0].y.toFixed(2)}`;

    for (
      let index = 1;
      index < points.length - 1;
      index += 1
    ) {
      const point = points[index];
      const nextPoint = points[index + 1];

      const midpointX =
        (point.x + nextPoint.x) / 2;

      const midpointY =
        (point.y + nextPoint.y) / 2;

      path +=
        ` Q ${point.x.toFixed(2)} ` +
        `${point.y.toFixed(2)} ` +
        `${midpointX.toFixed(2)} ` +
        `${midpointY.toFixed(2)}`;
    }

    const lastPoint =
      points[points.length - 1];

    path +=
      ` Q ${lastPoint.x.toFixed(2)} ` +
      `${lastPoint.y.toFixed(2)} ` +
      `${lastPoint.x.toFixed(2)} ` +
      `${lastPoint.y.toFixed(2)}`;

    return path;
  }

  _createLimitGraphPath() {
    const graphPoints =
      this._limitHistory;

    if (!graphPoints.length) {
      return {
        linePath: "",
        fillPath: "",
      };
    }

    const now = Date.now();
    const suppressThreshold =
      this._config.max -
      (this._config.max - this._config.min) *
        0.03;

    const start =
      now -
      this._config.hours_to_show *
        60 *
        60 *
        1000;

    const points = graphPoints.map(
      (point) => {
        const x =
          ((point.timestamp - start) /
            (now - start)) *
          100;

        const scaledValue =
          point.value /
          this._config.limit_divider;

        const y =
          40 -
          ((scaledValue - this._config.min) /
            (this._config.max -
              this._config.min)) *
            40;

        return {
          x: Math.max(
            0,
            Math.min(100, x)
          ),

          y: Math.max(
            0,
            Math.min(40, y)
          ),

          suppressed:
            scaledValue >= suppressThreshold,
        };
      }
    );

    let linePath = "";
    let fillPath = "";

    for (
      let index = 0;
      index < points.length;
      index += 1
    ) {

      if (points[index].suppressed) {
        continue;
      }

      const startX =
        index === 0
          ? 0
          : points[index].x;

      const endX =
        index + 1 < points.length
          ? points[index + 1].x
          : 100;

      const y =
        points[index].y;

      linePath +=
        `M ${startX.toFixed(2)} ` +
        `${y.toFixed(2)} ` +
        `H ${endX.toFixed(2)} `;

      fillPath +=
        `M ${startX.toFixed(2)} 0 ` +
        `H ${endX.toFixed(2)} ` +
        `V ${y.toFixed(2)} ` +
        `H ${startX.toFixed(2)} Z `;
    }

    return {
      linePath: linePath.trim(),
      fillPath: fillPath.trim(),
    };
  }
  
  _render() {
    if (!this._config) {
      return;
    }

    const stateObject =
      this._hass?.states?.[
      this._config.entity
      ];

    let numericValue = Number(
      stateObject?.state
    );
    if (Number.isNaN(numericValue)) numericValue = 0;

    const percentage = Number.isFinite(
      numericValue
    )
      ? this._calculatePercentage(
        numericValue
      )
      : 0;

    this._card.style.setProperty(
      "--solar-panel-fill-percentage",
      `${percentage}%`
    );

    this._value.textContent = this._formatState(stateObject);

    this._card.setAttribute(
      "aria-label",
      `${this._config.entity}: ` +
      this._value.textContent
    );

    this._renderGraph();
  }

  _renderGraph() {
    const graphPath =
      this._createGraphPath();

    const {
      linePath: limitGraphPath,
      fillPath: limitGraphFillPath,
    } = this._createLimitGraphPath();

    this._graph.replaceChildren();

    if (graphPath || limitGraphPath) {
      const svgNamespace =
        "http://www.w3.org/2000/svg";

      const svg =
        document.createElementNS(
          svgNamespace,
          "svg"
        );

      svg.setAttribute(
        "viewBox",
        "0 0 100 40"
      );

      svg.setAttribute(
        "preserveAspectRatio",
        "none"
      );

      svg.setAttribute(
        "aria-label",
        "Sensor history"
      );

      const baseline =
        document.createElementNS(
          svgNamespace,
          "line"
        );

      baseline.setAttribute(
        "class",
        "graph-baseline"
      );

      baseline.setAttribute("x1", "0");
      baseline.setAttribute("y1", "40");
      baseline.setAttribute("x2", "100");
      baseline.setAttribute("y2", "40");

      const path =
        document.createElementNS(
          svgNamespace,
          "path"
        );

      path.setAttribute(
        "class",
        "graph-line"
      );

      path.setAttribute("d", graphPath);

      const fill = document.createElementNS(
        svgNamespace,
        "path"
      );

      fill.setAttribute(
        "class",
        "graph-fill"
      );

      fill.setAttribute(
        "d",
        `${graphPath} L 100 40 L 0 40 Z`
      );

      svg.append(baseline);

      if (graphPath) {
        svg.append(fill, path);
      }

      if (limitGraphFillPath) {
        const limitFill =
          document.createElementNS(
            svgNamespace,
            "path"
          );

        limitFill.setAttribute(
          "class",
          "graph-limit-fill"
        );

        limitFill.setAttribute(
          "d",
          limitGraphFillPath
        );

        limitFill.setAttribute(
          "fill",
          "red"
        );

        limitFill.setAttribute(
          "fill-opacity",
          "0.3"
        );

        limitFill.setAttribute(
          "stroke",
          "none"
        );

        svg.append(limitFill);
      }

      if (limitGraphPath) {
        const limitPath =
          document.createElementNS(
            svgNamespace,
            "path"
          );

        limitPath.setAttribute(
          "class",
          "graph-limit-line"
        );

        limitPath.setAttribute(
          "d",
          limitGraphPath
        );

        limitPath.setAttribute(
          "fill",
          "none"
        );

        limitPath.setAttribute(
          "stroke",
          "red"
        );

        limitPath.setAttribute(
          "stroke-width",
          "1"
        );

        limitPath.setAttribute(
          "vector-effect",
          "non-scaling-stroke"
        );

        svg.append(limitPath);
      }

      this._graph.append(svg);

      return;
    }

    const message =
      document.createElement("div");

    message.className = "message";

    if (this._error) {
      message.textContent = this._error;
    } else if (this._loadingHistory) {
      message.textContent =
        "Loading history…";
    } else {
      message.textContent =
        "No numeric history";
    }

    this._graph.append(message);
  }

  _showMoreInfo() {
    if (!this._config) {
      return;
    }

    this.dispatchEvent(
      new CustomEvent("hass-more-info", {
        bubbles: true,
        composed: true,
        detail: {
          entityId:
            this._config.entity,
        },
      })
    );
  }
}

customElements.define(
  "solar-panel-card",
  SolarPanelCard
);

window.customCards =
  window.customCards || [];

window.customCards.push({
  type: "solar-panel-card",
  name: "Solar Panel Card",
  description:
    "A vertically filled sensor gauge with a trend graph.",
  preview: false,
});
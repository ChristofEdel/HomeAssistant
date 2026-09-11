class HistoryManagerCard extends HTMLElement {


  constructor() {
    super();
    this.attachShadow({ mode: "open" });

    this._dbStats = null;
    this._tableData = null;
    this._treeData = null;
    this._configData = null;
    this._expanded = new Set();

    this._loading = false;
    this._message = "";
    this._activeTab = "devices";

    // Persistent shadow root content to avoid reloading styles on each render
    // this prevents the flickering effect when switching tabs or updating stats
    this.shadowRoot.innerHTML = `
      <link
        rel="stylesheet"
        href="/api/history_manager/static/history-manager-card.css"
      >
      <div id="root"></div>
    `;
  }

  set hass(hass) {
    this._hass = hass;

    if (!this._loaded) {
      this._loaded = true;
      this._load();
    }
  }

  setConfig(config) {
    this._config = {
      title: "History Manager",
      ...config,
    };
    this._render();
  }

  /////////////////////////////////////////////////////////////////////////////////////////////////
  // #region Requests for Data
  //

  _push_request(request, setLoading, render) {
    setLoading(true);
    render();

    return this._hass.connection
      .sendMessagePromise({ type: request })
      .then((result) => {
        setLoading(false);
        render(result);
      })
      .catch((err) => {
        console.error(`History Manager: ${request} failed`, err);
        this._message += `Error loading data from ${request}: ${err?.message || err}\n`;
      })
      .finally(() => {
        setLoading(false);
      });
  }

  _setLoading(loading) {
    this._loading = loading;

    const button = this.shadowRoot.getElementById("load-button");
    if (button) {
      button.disabled = loading;
    }
  }

  async _load() {
    if (!this._hass || this._loading) return;

    this._setLoading(true);
    this._message = "";

    try {
      await Promise.allSettled([
        this._push_request(
          "history_manager/get_config",
          (loading) => { this._loadingConfigData = loading },
          (result) => { this._renderConfigData(result) }
        ),
        this._push_request(
          "history_manager/get_entity_tree",
          (loading) => { this._loadingTreeData = loading },
          (result) => { this._renderTreeData(result); this._renderStats(result) }
        ),
        this._push_request(
          "history_manager/analyze_db",
          (loading) => { this._loadingDbStats = loading },
          (result) => { this._dbStats = result; this._renderStats(result?.stats); }
        ),
        this._push_request(
          "history_manager/get_table_data",
          (loading) => { this._loadingTableData = loading },
          (result) => { this._renderTableData(result); }
        )
      ]);
    } finally {
      this._setLoading(false);
    }
  }
  // #endregion
  /////////////////////////////////////////////////////////////////////////////////////////////////


  /////////////////////////////////////////////////////////////////////////////////////////////////
  // #region Rendering
  //
  
  _renderStatBox(label, statName) {
    return `
      <div class="stat-box">
        <div class="stat-label">${this._escapeHtml(label)}</div>
        <div class="stat-value" data-stat="${this._escapeHtml(statName)}">...</div>
      </div>
    `;
  }

  _renderStats(data) {
    if (data == null) return;

    const formatters = {
      generated_at:          (value) => this._formatDate(value),
      db_size_bytes:         (value) => this._formatBytes(value),
      db_reclaimable_bytes:  (value) => this._formatBytes(value),
      db_path:               (value) => value,
      default:               (value) => this._formatNumber(value),
    };

    this.shadowRoot
      .querySelectorAll("[data-stat]")
      .forEach((element) => {
        const name = element.dataset.stat;

        if (!Object.hasOwn(data, name)) return;

        const value = data[name];

        if (value !== null && typeof value === "object") return;

        const formatted = formatters[name]
          ? formatters[name](value)
          : formatters.default(value);

        element.textContent = formatted;
      });
  }
  
  _renderTableData(data) {
    if (data !== undefined) {
      this._tableData = data?.table_data ?? [];
    }

    if (this._activeTab != "database") return;
    const container = this.shadowRoot.getElementById("table-data");
    if (!container) return;

    let rows;

    if (this._tableData === null || this._loadingTableData) {
      rows = `<tr><td colspan="4">Loading...</td></tr>`;
    } 
    else if (!this._tableData.length) {
      rows = `<tr><td colspan="4">No table data available.</td></tr>`;
    } 
    else {
      rows = this._tableData.map((table) => `
        <tr>
          <td><code>${this._escapeHtml(table.table)}</code></td>
          <td class="right gap-before">${this._formatNumber(table.rows)}</td>
          <td class="right gap-before">${this._formatBytes(table.bytes)}</td>
          <td class="right gap-before">${Number(table.percent || 0).toFixed(1)}%</td>
        </tr>
      `).join("");
    }

    container.innerHTML = `
      <div class="table-section">
        <h2>Database tables</h2>

        <table class="db-table">
          <thead>
            <tr>
              <th>Table</th>
              <th class="right gap-before">Rows</th>
              <th class="right gap-before">Size</th>
              <th class="right gap-before">%</th>
            </tr>
          </thead>

          <tbody>
            ${rows}
          </tbody>
        </table>
      </div>
    `;
  }

  _renderConfigData(data) {
    if (data !== undefined) {
      this._configData = data;
    }

    if (this._activeTab != "config") return;
    const container = this.shadowRoot.getElementById("config-data");
    if (!container) return;

    if (this._configData === null || this._loadingConfigData) {
      container.innerHTML = "Loading...";
    } 
    else {
      container.innerHTML = 
        `<h2>Configuration</h2>`+
        `<pre>${this._escapeHtml(this._configData.config_yaml ?? "-- EMPTY --")}</pre>` +
        `<hr/><h2>Short Retention Entities</h2>`+
        `<pre>${this._escapeHtml(this._configData.short_entities.join('\n'))}</pre>` +
        `<hr/><h2>Unrecorded Entities</h2>`+
        `<pre>${this._escapeHtml(this._configData.unrecorded_entities.join('\n'))}</pre>`;
    } 
  }

  _renderTreeData(data) {
    if (data !== undefined) {
      this._treeData = data ?? {};
    }
    this._renderDeviceData();
    this._renderStandaloneData();
    this._renderObsoleteData();
  }

  _renderDeviceData() {
    if (!this._activeTab == "devices") return;
    const container = this.shadowRoot.getElementById("device-data");
    if (!container) return;

    const totalRecordCount = this._treeData?.total_record_count;

    let rows;

    if (this._treeData === null || this._loadingTreeData) {
      rows = `
        <tr>
          <td colspan="3">Loading...</td>
        </tr>
      `;
    } else {
      const devices = this._treeData.device_tree.devices || [];

      if (!devices.length) {
        rows = `
          <tr>
            <td colspan="3">No devices to display.</td>
          </tr>
        `;
      } else {
        rows = devices.map((device) => {
          const expanded = this._expanded.has(device.device_id);
          const entities = device.entities || [];

          const deviceRow = `
            <tr class="device-row">
              <td>
                  <button
                    class="caret"
                    data-toggle-device="${this._escapeHtml(device.device_id)}"
                    aria-label="Expand or collapse device"
                  >
                    <span class="caret-icon ${expanded ? "expanded" : ""}">›</span>
                  </button>
              </td>
              <td>
                <div class="name-cell">
                  <div>
                    <div class="primary-name">
                      ${this._escapeHtml(device.name || device.device_id)}
                    </div>
                    <div class="secondary-name">
                      ${this._escapeHtml(
                        [device.manufacturer || "", device.model || ""]
                          .filter(Boolean)
                          .join(" · ")
                      )}
                    </div>
                  </div>
                </div>
              </td>
              <td class="gap-before">
                <div class="entity-recorder-counts">
                  <div class="long"  ${this._display_if(device.recorder_standard_count > 0)}>${device.recorder_standard_count}</div>
                  <div class="short" ${this._display_if(device.recorder_short_count > 0)}>${device.recorder_short_count}</div>
                  <div class="off"   ${this._display_if(device.recorder_off_count > 0)}>${device.recorder_off_count}</div>
                </div>
              </td>
              <td class="right gap-before">
                ${this._formatNumber(device.record_count || 0)}
              </td>
              <td class="right gap-before">
                ${this._formatPercent(
                  device.record_count || 0,
                  totalRecordCount
                )}
              </td>
            </tr>
          `;

          if (!expanded) {
            return deviceRow;
          }

          const entityRows = entities.map((entity) => `
            <tr class="entity-row">
              <td class="no-border">
              </td>
              <td>
                <div class="entity-name-cell">
                  <div class="primary-name">
                    ${this._escapeHtml(entity.entity_id)}
                  </div>
                  <div class="secondary-name">
                    ${this._escapeHtml(entity.name || "")}
                  </div>
                </div>
              </td>
              <td class="gap-before">
                <div class="entity-recorder-state short" ${this._display_if(entity.recorder_short)}>short</div>
                <div class="entity-recorder-state long"  ${this._display_if(entity.recorder_standard)}></div>
                <div class="entity-recorder-state off"   ${this._display_if(entity.recorder_off)}>off</div>
              </td>
              <td class="right gap-before">
                ${this._formatNumber(entity.record_count || 0)}
              </td>
              <td class="right gap-before">
                ${this._formatPercent(
                  entity.record_count || 0,
                  totalRecordCount
                )}
              </td>
            </tr>
          `).join("");

          return deviceRow + entityRows;
        }).join("");
      }
    }

    container.innerHTML = `
      <table class="device-table">
        <thead>
          <tr>
            <th colspan=2>Name</th>
            <th class="gap-before">Entities</div>
            <th class="gap-before">Records</th>
            <th class="right gap-before">%</th>
          </tr>
        </thead>

        <tbody>
          ${rows}
        </tbody>
      </table>
    `;

    container.querySelectorAll("[data-toggle-device]").forEach((button) => {
      button.addEventListener("click", () => {
        const deviceId = button.dataset.toggleDevice;

        if (this._expanded.has(deviceId)) {
          this._expanded.delete(deviceId);
        } else {
          this._expanded.add(deviceId);
        }

        this._renderDeviceData();
      });
    });
  }

  _renderEntityTable(containerId, data) {
    const container = this.shadowRoot.getElementById(containerId);
    if (!container) return;

    const totalRecordCount = this._treeData?.total_record_count;

    let rows;
    if (this._treeData === null || this._loadingTreeData) {
      rows = `<tr><td colspan="5">Loading...</td></tr>`;
    } else if (!data.entities?.length) {
      rows = `<tr><td colspan="5">No entities to display.</td></tr>`;
    } else {
      rows = data.entities.map((entity) => `
        <tr class="entity-row">
          <td>
            <input
              type="checkbox"
              class="entity-select"
              data-entity-id="${this._escapeHtml(entity.entity_id)}"
            >
          </td>

          <td>
            <div class="entity-name-cell">
              <div class="primary-name">
                ${this._escapeHtml(entity.entity_id)}
              </div>
              <div class="secondary-name">
                ${this._escapeHtml(entity.name || "")}
              </div>
            </div>
          </td>

          <td class="gap-before">
            <div class="entity-recorder-state short" ${this._display_if(entity.recorder_short)}>short</div>
            <div class="entity-recorder-state long"  ${this._display_if(entity.recorder_standard)}></div>
            <div class="entity-recorder-state off"   ${this._display_if(entity.recorder_off)}>off</div>
          </td>

          <td class="right gap-before">
            ${this._formatNumber(entity.record_count || 0)}
          </td>

          <td class="right gap-before">
            ${this._formatPercent(
              entity.record_count || 0,
              totalRecordCount
            )}
          </td>
        </tr>
      `).join("");
    }

    container.innerHTML = `
      <div class="device-section">
        <table class="device-table">
          <thead>
            <tr>
              <th></th>
              <th>Name</th>
              <th class="right gap-before" colspan=2>Records</th>
              <th class="right gap-before">%</th>
            </tr>
            <tr>
              <td>
                <input type="checkbox" class="entity-select-all">
              </td>
              <td>Total</td>
              <td class="gap-before">
                <div class="entity-recorder-counts">
                  <div class="long"  ${this._display_if(data.recorder_standard_count > 0)}>${data.recorder_standard_count}</div>
                  <div class="short" ${this._display_if(data.recorder_short_count > 0)}>${data.recorder_short_count}</div>
                  <div class="off"   ${this._display_if(data.recorder_off_count > 0)}>${data.recorder_off_count}</div>
                </div>
              </td>
              <td class="right gap-before">
                ${this._formatNumber(data.record_count || 0)}
              </td>
              <td class="right gap-before">
                ${this._formatPercent(
                  data.record_count || 0,
                  totalRecordCount
                )}
              </td>
          </thead>

          <tbody>
            ${rows}
          </tbody>
        </table>
      </div>
    `;

    const selectAll = container.querySelector(".entity-select-all");

    selectAll?.addEventListener("change", () => {
      container.querySelectorAll(".entity-select").forEach((checkbox) => {
        checkbox.checked = selectAll.checked;
      });
    });
  }

  _renderStandaloneData() {
    if (this._activeTab != "standalone") return;
    this._renderEntityTable(
      "standalone-data",
      this._treeData?.standalone_entities
    );
  }

  _renderObsoleteData() {
    if (this._activeTab != "obsolete") return;
    this._renderEntityTable(
      "obsolete-data",
      this._treeData?.obsolete_entities
    );
  }

  _get_selected_entities(containerId) {
    const container = this.shadowRoot.getElementById(containerId);
    if (!container) return [];

    return Array.from(
      container.querySelectorAll(".entity-select:checked")
    ).map((checkbox) => checkbox.dataset.entityId);
  }

  _render() {
    const root = this.shadowRoot.getElementById("root");

    root.innerHTML = `
      <ha-card>
        <div class="content">
          <div class="toolbar-head">
            <div class="title-row">
              ${
                this._config?.icon
                  ? `<ha-icon icon="${this._escapeHtml(this._config.icon)}"></ha-icon>`
                  : ""
              }
              <div class="title">
                ${this._escapeHtml(this._config?.title || "History Manager")}
              </div>
            </div>

            <button
              class="action-button"
              id="load-button"
              title="Refresh"
              aria-label="Refresh"
              ${this._loading ? "disabled" : ""}
            >
              <ha-icon icon="mdi:refresh"></ha-icon>
            </button>
          </div>

          ${
            this._message
              ? `<div class="message">${this._escapeHtml(this._message)}</div>`
              : ""
          }
        </div>

        <div class="tabs">
          <button class="tab ${this._activeTab === "devices" ? "active" : ""}" data-tab="devices">
            Devices
          </button>
          <button class="tab ${this._activeTab === "standalone" ? "active" : ""}" data-tab="standalone">
            Standalone 
          </button>
          <button class="tab ${this._activeTab === "obsolete" ? "active" : ""}" data-tab="obsolete">
            Obsolete
          </button>
          <button class="tab ${this._activeTab === "database" ? "active" : ""}" data-tab="database">
            Database
          </button>
          <button class="tab ${this._activeTab === "config" ? "active" : ""}" data-tab="config">
            Config
          </button>
        </div>

        <div class="card">
          ${
            this._activeTab === "devices"
              ? `
                <div class="stats-grid">
                  ${this._renderStatBox("DB size",            "db_size_bytes")}
                  ${this._renderStatBox("Total",              "total_record_count")}
                  ${this._renderStatBox("Standard Retention", "recorder_standard_record_count")}
                  ${this._renderStatBox("Short Retention",    "recorder_short_record_count")}
                  ${this._renderStatBox("No Retention",       "recorder_off_record_count")}
                </div>
                <div class='table-container'>
                  <div class='table-container-header'>
                    <h2>Devices</h2>
                    <button onclick="this.getRootNode().host._purge_entities()">
                      Start Purge
                    </button>
                  </div>
                  <div id="device-data" class='table-container-data'></div>
                </div>
              `
              : ""
          }

          ${
            this._activeTab === "standalone"
              ? `
                <div class="stats-grid">
                  ${this._renderStatBox("DB size",            "db_size_bytes")}
                  ${this._renderStatBox("Total",              "total_record_count")}
                  ${this._renderStatBox("Standard Retention", "recorder_standard_record_count")}
                  ${this._renderStatBox("Short Retention",    "recorder_short_record_count")}
                  ${this._renderStatBox("No Retention",       "recorder_off_record_count")}
                </div>
                <div class='table-container'>
                  <div class='table-container-header'>
                    <h2>Standalone Entities</h2>
                    <button onclick="this.getRootNode().host._purge_entities()">
                      Start Purge
                    </button>
                  </div>
                  <div id="standalone-data" class='table-container-data'></div>
                </div>
              `
              : ""
          }

          ${
            this._activeTab === "obsolete"
              ? `
                <div class="stats-grid">
                  ${this._renderStatBox("DB size",            "db_size_bytes")}
                  ${this._renderStatBox("Total",              "total_record_count")}
                  ${this._renderStatBox("Standard Retention", "recorder_standard_record_count")}
                  ${this._renderStatBox("Short Retention",    "recorder_short_record_count")}
                  ${this._renderStatBox("No Retention",       "recorder_off_record_count")}
                </div>
                <div class='table-container'>
                  <div class='table-container-header'>
                    <h2>Obsolete Entities</h2>
                    <button onclick="
                      this.getRootNode().host._clear_entities(
                        this.getRootNode().host._get_selected_entities(
                          'obsolete-data'
                        )
                      )
                    ">
                      Clear selected
                    </button>
                  </div>
                  <div id="obsolete-data" class='table-container-data'></div>
                </div>
              `
              : ""
          }

          ${
            this._activeTab === "database"
              ? `
                <div class="stats-grid">
                  ${this._renderStatBox("DB size", "db_size_bytes")}
                  ${this._renderStatBox("Reclaimable (estimate)", "db_reclaimable_bytes")}
                  ${this._renderStatBox("Database", "db_path")}
                </div>
                <div id="table-data">
                </div>
              `
              : ""
          }

          ${
            this._activeTab === "config"
              ? `
                <div id="config-data">
                </div>
              `
              : ""
          }
        </div>
      </ha-card>
    `;

    this.shadowRoot
      .getElementById("load-button")
      ?.addEventListener("click", () => this._load());

    this.shadowRoot.querySelectorAll("[data-tab]").forEach((tab) => {
      tab.addEventListener("click", () => {
        this._activeTab = tab.dataset.tab;
        this._render();
      });
    });

    this._renderStats(this._treeData);
    this._renderStats(this._dbStats?.stats);
    this._renderTableData();
    this._renderTreeData();
    this._renderConfigData();
  }
  // #endregion Rendering
  /////////////////////////////////////////////////////////////////////////////////////////////////


  _clear_entities(entities) {
    if (!entities?.length) return;
    console.log("Clearing entities:\n" + entities.join('\n'))
    
    this._hass.callService("recorder", "purge_entities", {
      keep_days: 0,
      entity_id: entities,
    });
  }

  _purge_entities(entities) {

    this._hass.callService("history_manager", "purge_short_entities", {});
    this._hass.callService("history_manager", "purge_unrecorded_entities", {});

  }

  /////////////////////////////////////////////////////////////////////////////////////////////////
  // #region Formatting helpers
  //

  _formatNumber(value) {
    return new Intl.NumberFormat("en-US").format(Number(value || 0));
  }

  _formatBytes(value) {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";

    const units = ["B", "KB", "MB", "GB", "TB"];
    let size = bytes;
    let unitIndex = 0;

    while (size >= 1024 && unitIndex < units.length - 1) {
      size /= 1024;
      unitIndex += 1;
    }

    const decimals = unitIndex === 0 ? 0 : size >= 10 ? 1 : 2;
    return `${size.toFixed(decimals)} ${units[unitIndex]}`;
  }

  _formatMB(value) {
    return (Number(value || 0) / (1024 * 1024)).toFixed(2);
  }

  _formatDate(value) {
    if (!value) return "—";

    try {
      return new Date(value).toLocaleString();
    } catch (_err) {
      return value;
    }
  }

  _formatPercent(part, total) {
    if (total === undefined || total === null) return "...";
    if (!total) return "0%";
    return `${((Number(part || 0) / total) * 100).toFixed(1)}%`;
  }

  _escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  _display_if(value) {
    if (value) return ''
    return " style='display: none'"
  }

  // #endregion
  /////////////////////////////////////////////////////////////////////////////////////////////////

}

customElements.define("history-manager-card", HistoryManagerCard);

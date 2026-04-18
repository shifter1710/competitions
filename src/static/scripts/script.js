class Main {
    constructor() {
        this.currentReportUrl = null;
        this.draggedColumnKey = null;
        this.initializeDomReferences();
        this.createInstances();
        this.hangEvents();
        this.initTableFeatures();
    }

    initializeDomReferences() {
        this.contentWrapper = document.querySelector(".content-wrapper");
        this.importForm = document.querySelector(".import-form");
        this.manualForm = document.querySelector(".manual-form");
        this.manualFormButton = document.querySelector(".manual-form__button");
        this.manualFormCancelButton = document.querySelector(".manual-form__cancel-button");
        this.manualDateInput = document.querySelector('.manual-form [name="date"]');
        this.customFieldInputs = Array.from(document.querySelectorAll(".custom-field-input"));
        this.reportForm = document.querySelector(".filter-form");
        this.fileInput = document.querySelector(".import-form__input");
        this.importButton = document.querySelector(".import-form__button");
        this.loader = document.querySelector(".loader");
        this.inputName = document.querySelector(".filter__name");
        this.dateFromInput = document.querySelector(".filter__date-from");
        this.dateToInput = document.querySelector(".filter__date-to");
        this.levelSelectWrapper = document.querySelector(".filter__level-wrapper");
        this.positionSelectWrapper = document.querySelector(".filter__position-wrapper");
        this.cleanFilterButton = document.querySelector(".filter-form__clean-button");
        this.cleanButton = document.querySelector(".clean-button");
        this.exportButton = document.querySelector(".export-button");
        this.tableLayoutButton = document.querySelector(".table-layout-button");
        this.tableLayoutResetButton = document.querySelector(".table-layout-reset-button");
        this.tableLayoutCloseButton = document.querySelector(".table-layout-close-button");
        this.tableControlsPanel = document.querySelector(".table-controls-panel");
        this.tableColumnsManager = document.querySelector(".table-columns-manager");
        this.dateRangePickerElement = document.querySelector('.datetime');
        this.tableCard = document.querySelector(".table-card");
        this.tableElement = document.querySelector(".interactive-table");
        this.filterIsApplied = Boolean(this.tableCard && this.tableCard.dataset.tableView === "report");
    }

    createInstances() {
        if (this.dateRangePickerElement) {
            this.dateRangePicker = new DateRangePicker(this.dateRangePickerElement, {
                format: "dd.mm.yyyy"
            });
        }
        if (this.manualDateInput) {
            this.manualDatePicker = new Datepicker(this.manualDateInput, {
                autohide: true,
                format: "dd.mm.yyyy"
            });
        }
        this.customFieldInputs
            .filter((input) => input.dataset.customFieldType === "date")
            .forEach((input) => {
                new Datepicker(input, {
                    autohide: true,
                    format: "dd.mm.yyyy"
                });
            });
        if (document.querySelector(".filter__level")) {
            this.levelSelect = NiceSelect.bind(document.querySelector(".filter__level"), {searchable: true, searchtext: "Найти"});
        }
        if (document.querySelector(".filter__position")) {
            this.positionSelect = NiceSelect.bind(document.querySelector(".filter__position"), {searchable: true, searchtext: "Найти"});
        }
    }

    destroyInstances() {
        if (this.levelSelect) {
            this.levelSelect.destroy();
            this.levelSelect = null;
        }
        if (this.positionSelect) {
            this.positionSelect.destroy();
            this.positionSelect = null;
        }
    }

    hangEvents() {
        if (this.importForm) {
            this.importForm.addEventListener("submit", (event) => this.handleSubmitImportForm(event));
        }
        if (this.manualForm) {
            this.manualForm.addEventListener("submit", (event) => this.handleSubmitManualForm(event));
        }
        if (this.manualFormCancelButton) {
            this.manualFormCancelButton.addEventListener("click", () => this.resetManualForm());
        }
        if (this.manualDateInput) {
            this.manualDateInput.addEventListener("input", (event) => this.handleManualDateInput(event));
            this.manualDateInput.addEventListener("blur", () => this.normalizeDateInput(this.manualDateInput));
        }
        this.customFieldInputs
            .filter((input) => input.dataset.customFieldType === "date")
            .forEach((input) => {
                input.addEventListener("input", (event) => this.handleManualDateInput(event));
                input.addEventListener("blur", () => this.normalizeDateInput(input));
            });
        if (this.fileInput) {
            this.fileInput.addEventListener("change", () => this.setDisabledImportButton(false));
        }
        if (this.reportForm) {
            this.reportForm.addEventListener("submit", (event) => this.handleSubmitReportForm(event));
        }
        if (this.cleanFilterButton) {
            this.cleanFilterButton.addEventListener("click", () => this.handleResetFilterButton());
        }
        if (this.exportButton) {
            this.exportButton.addEventListener("click", () => this.handleClickExportButton());
        }
        if (this.tableLayoutButton) {
            this.tableLayoutButton.addEventListener("click", () => this.toggleTableLayoutPanel());
        }
        if (this.tableLayoutCloseButton) {
            this.tableLayoutCloseButton.addEventListener("click", () => this.toggleTableLayoutPanel(false));
        }
        if (this.tableLayoutResetButton) {
            this.tableLayoutResetButton.addEventListener("click", () => this.resetTableLayout());
        }
        if (this.cleanButton) {
            this.cleanButton.addEventListener("click", () => this.cleanDb());
        }
        this.bindContentWrapperEvents();
    }

    bindContentWrapperEvents() {
        if (!this.contentWrapper || this.contentWrapper.dataset.bound === "true") {
            return;
        }
        this.contentWrapper.dataset.bound = "true";
        this.contentWrapper.addEventListener("click", (event) => this.handleContentWrapperClick(event));
    }

    getCurrentView() {
        if (!this.tableCard) {
            return "index";
        }
        return this.tableCard.dataset.tableView || "index";
    }

    getTableStateStorageKey() {
        return `competitions-table-state:${this.getCurrentView()}`;
    }

    getDefaultColumnKeys() {
        if (!this.tableElement) {
            return [];
        }
        return Array.from(this.tableElement.querySelectorAll("thead th")).map((header) => header.dataset.columnKey);
    }

    readTableState() {
        const rawState = localStorage.getItem(this.getTableStateStorageKey());
        const defaultOrder = this.getDefaultColumnKeys();
        if (!rawState) {
            return { order: defaultOrder, hidden: [] };
        }
        try {
            const parsed = JSON.parse(rawState);
            const normalizedOrder = [
                ...parsed.order.filter((key) => defaultOrder.includes(key)),
                ...defaultOrder.filter((key) => !parsed.order.includes(key)),
            ];
            const hidden = parsed.hidden.filter((key) => defaultOrder.includes(key));
            return { order: normalizedOrder, hidden };
        } catch {
            return { order: defaultOrder, hidden: [] };
        }
    }

    saveTableState(state) {
        localStorage.setItem(this.getTableStateStorageKey(), JSON.stringify(state));
    }

    setDisabledImportButton(state) {
        if (!this.importButton) {
            return;
        }
        this.importButton.disabled = state;
    }

    cleanFileInput() {
        if (!this.fileInput) {
            return;
        }
        this.setDisabledImportButton(true);
        this.fileInput.value = "";
    }

    toggleTableLayoutPanel(forceState = null) {
        if (!this.tableControlsPanel) {
            return;
        }
        const nextState = forceState === null ? this.tableControlsPanel.classList.contains("d-none") : forceState;
        this.tableControlsPanel.classList.toggle("d-none", !nextState);
    }

    resetTableLayout() {
        if (!this.tableElement) {
            return;
        }
        localStorage.removeItem(this.getTableStateStorageKey());
        this.applyTableState();
    }

    handleClickExportButton() {
        this.exportCurrentTable();
    }

    handleResetFilterButton() {
        this.resetFilter();
        this.currentReportUrl = null;
        this.filterIsApplied = false;
        this.refreshIndexContent();
    }

    resetFilter() {
        if (!this.inputName) {
            return;
        }
        this.inputName.value = "";
        this.levelSelectWrapper.querySelector('li.option[data-value=""]').click();
        this.positionSelectWrapper.querySelector('li.option[data-value=""]').click();
        this.destroyInstances();
        this.createInstances();
        this.dateFromInput.value = "";
        this.dateToInput.value = "";
    }

    handleSubmitImportForm(event) {
        event.preventDefault();
        const formData = new FormData(this.importForm);
        this.importFile(formData);
    }

    handleSubmitManualForm(event) {
        event.preventDefault();
        const formData = new FormData(this.manualForm);
        this.createCompetition(formData);
    }

    handleManualDateInput(event) {
        const digits = event.target.value.replace(/\D/g, "").slice(0, 8);
        const parts = [];

        if (digits.length > 0) {
            parts.push(digits.slice(0, 2));
        }
        if (digits.length > 2) {
            parts.push(digits.slice(2, 4));
        }
        if (digits.length > 4) {
            parts.push(digits.slice(4, 8));
        }

        event.target.value = parts.join(".");
    }

    normalizeDateInput(input) {
        const digits = input.value.replace(/\D/g, "").slice(0, 8);
        if (digits.length <= 4) {
            return;
        }

        const day = digits.slice(0, 2);
        const month = digits.slice(2, 4);
        const year = digits.slice(4, 8);
        input.value = [day, month, year].filter(Boolean).join(".");
    }

    resetManualForm() {
        if (!this.manualForm) {
            return;
        }
        this.manualForm.reset();
        this.manualForm.querySelector('[name="record_id"]').value = "";
        this.manualForm.querySelector(".title").textContent = "Добавить запись";
        this.manualFormButton.textContent = "Добавить";
    }

    startEditCompetition(dataset) {
        if (!this.manualForm) {
            return;
        }

        const extraData = dataset.extraJson ? JSON.parse(dataset.extraJson) : {};
        this.manualForm.querySelector('[name="record_id"]').value = dataset.recordId;
        this.manualForm.querySelector('[name="student_name"]').value = dataset.studentName;
        this.manualForm.querySelector('[name="student_sex"]').value = dataset.studentSex;
        this.manualForm.querySelector('[name="institute"]').value = dataset.institute;
        this.manualForm.querySelector('[name="group"]').value = dataset.group;
        this.manualForm.querySelector('[name="course"]').value = dataset.course;
        this.manualForm.querySelector('[name="sport"]').value = dataset.sport;
        this.manualForm.querySelector('[name="date"]').value = dataset.date;
        this.manualForm.querySelector('[name="level"]').value = dataset.level;
        this.manualForm.querySelector('[name="name"]').value = dataset.name;
        this.manualForm.querySelector('[name="position"]').value = dataset.position;
        this.customFieldInputs.forEach((input) => {
            input.value = extraData[input.dataset.customFieldKey] || "";
        });
        this.manualForm.querySelector(".title").textContent = "Редактировать запись";
        this.manualFormButton.textContent = "Сохранить";
        this.manualForm.scrollIntoView({behavior: "smooth", block: "start"});
    }

    handleContentWrapperClick(event) {
        const editButton = event.target.closest(".competition-edit-button");
        if (editButton) {
            this.startEditCompetition(editButton.dataset);
            return;
        }

        const deleteButton = event.target.closest(".competition-delete-button");
        if (deleteButton) {
            this.deleteCompetition(deleteButton.dataset.recordId);
        }
    }

    prepareParamsForReport() {
        const params = new URLSearchParams();
        const formData = new FormData(this.reportForm);
        Array.from(formData.entries()).forEach((field) => {
            const [fieldName, value] = field;
            if (value) {
                params.append(fieldName, value);
            }
        });
        return params.toString();
    }

    handleSubmitReportForm(event) {
        event.preventDefault();
        const params = this.prepareParamsForReport();
        this.getReport(params);
    }

    importFile(formData) {
        this.makeRequest({
            url: "/",
            options: {
                method: "POST",
                body: formData,
            },
            onSuccess: () => {
                alert("Файл успешно импортирован");
                this.cleanFileInput();
                this.refreshCurrentContent();
            },
            onError: (message) => alert(message || "Ошибка импорта")
        });
    }

    createCompetition(formData) {
        const recordId = formData.get("record_id");
        const url = recordId ? `/competition/${recordId}` : "/competition";
        this.makeRequest({
            url,
            options: {
                method: "POST",
                body: formData,
            },
            onSuccess: () => {
                alert(recordId ? "Запись успешно обновлена" : "Запись успешно добавлена");
                this.resetManualForm();
                this.refreshCurrentContent();
            },
            onError: (message) => alert(message || "Ошибка добавления записи")
        });
    }

    deleteCompetition(recordId) {
        const result = confirm("Удалить запись?");
        if (!result) {
            return;
        }
        this.makeRequest({
            url: `/competition/${recordId}/delete`,
            options: {
                method: "POST",
            },
            onSuccess: () => {
                alert("Запись удалена");
                this.resetManualForm();
                this.refreshCurrentContent();
            },
            onError: (message) => alert(message || "Ошибка удаления записи")
        });
    }

    getReport(params) {
        const url = "/report" + (params ? `?${params}` : "");
        this.setLoading(true);
        fetch(url, { method: "GET" })
            .then((response) => {
                if (!response.ok) {
                    throw new Error("Ошибка применения фильтра");
                }
                return response.text();
            })
            .then((html) => {
                this.replaceContentWrapper(html);
                this.currentReportUrl = url;
                this.filterIsApplied = true;
                this.setLoading(false);
            })
            .catch(() => {
                this.setLoading(false);
                alert("Ошибка применения фильтра. Попробуйте позже");
            });
    }

    cleanDb() {
        const result = confirm("Вы действительно хотите очистить базу данных?");
        if (!result) {
            return;
        }
        this.makeRequest({
            url: "/clean_db",
            options: {
                method: "POST"
            },
            onSuccess: () => {
                alert("База данных успешно очищена");
                this.refreshCurrentContent();
            },
            onError: (message) => alert(message || "Ошибка очистки базы данных")
        });
    }

    makeRequest({ url, options = {}, onSuccess = () => {}, onError = () => {} }) {
        this.setLoading(true);
        fetch(url, options)
            .then((response) => {
                if (response.redirected && response.url.includes("/login")) {
                    window.location.href = response.url;
                    throw new Error("Redirected to login");
                }
                if (response.status === 401) {
                    window.location.href = "/login";
                    throw new Error("Unauthorized");
                }
                if (!response.ok) {
                    return response.text().then((message) => {
                        throw new Error(message);
                    });
                }
                return response.text();
            })
            .then(() => {
                this.setLoading(false);
                onSuccess();
            })
            .catch((error) => {
                this.setLoading(false);
                if (error.message !== "Redirected to login" && error.message !== "Unauthorized") {
                    onError(error.message);
                }
            });
    }

    refreshCurrentContent() {
        if (this.currentReportUrl) {
            this.getReport(this.currentReportUrl.split("?")[1] || "");
            return;
        }
        this.refreshIndexContent();
    }

    refreshIndexContent() {
        this.setLoading(true);
        fetch("/", { method: "GET" })
            .then((response) => response.text())
            .then((html) => {
                const doc = new DOMParser().parseFromString(html, "text/html");
                const nextContent = doc.querySelector(".content-wrapper");
                if (nextContent && this.contentWrapper) {
                    this.contentWrapper.replaceWith(nextContent);
                    this.initializeDomReferences();
                    this.bindContentWrapperEvents();
                    this.initTableFeatures();
                }
                this.setLoading(false);
            })
            .catch(() => {
                this.setLoading(false);
                window.location.reload();
            });
    }

    replaceContentWrapper(html) {
        const doc = new DOMParser().parseFromString(html, "text/html");
        const nextContent = doc.querySelector(".content-wrapper") || doc.body.firstElementChild;
        if (!nextContent || !this.contentWrapper) {
            window.location.reload();
            return;
        }
        this.contentWrapper.replaceWith(nextContent);
        this.initializeDomReferences();
        this.bindContentWrapperEvents();
        this.initTableFeatures();
    }

    initTableFeatures() {
        this.tableCard = document.querySelector(".table-card");
        this.tableElement = document.querySelector(".interactive-table");
        if (!this.tableElement) {
            if (this.tableColumnsManager) {
                this.tableColumnsManager.innerHTML = "";
            }
            return;
        }
        this.applyTableState();
        this.bindTableHeaderEvents();
        this.renderColumnManager();
    }

    applyTableState() {
        if (!this.tableElement) {
            return;
        }
        const state = this.readTableState();
        state.order.forEach((columnKey, targetIndex) => this.moveColumn(columnKey, targetIndex));
        this.setHiddenColumns(state.hidden);
        this.renderColumnManager();
    }

    getHeaderByKey(columnKey) {
        return this.tableElement.querySelector(`thead th[data-column-key="${columnKey}"]`);
    }

    moveColumn(columnKey, targetIndex) {
        const header = this.getHeaderByKey(columnKey);
        if (!header) {
            return;
        }
        const headers = Array.from(this.tableElement.querySelectorAll("thead th"));
        const currentIndex = headers.indexOf(header);
        if (currentIndex === -1 || currentIndex === targetIndex) {
            return;
        }

        const rows = Array.from(this.tableElement.querySelectorAll("tr"));
        rows.forEach((row) => {
            const cells = Array.from(row.children);
            const cell = cells[currentIndex];
            if (!cell) {
                return;
            }
            if (targetIndex >= cells.length - 1) {
                row.appendChild(cell);
            } else {
                row.insertBefore(cell, cells[targetIndex]);
            }
        });
    }

    setHiddenColumns(hiddenKeys) {
        if (!this.tableElement) {
            return;
        }
        const hiddenSet = new Set(hiddenKeys);
        this.tableElement.querySelectorAll("[data-column-key]").forEach((cell) => {
            cell.classList.toggle("table-column-hidden", hiddenSet.has(cell.dataset.columnKey));
        });
    }

    bindTableHeaderEvents() {
        const headers = Array.from(this.tableElement.querySelectorAll("thead th"));
        headers.forEach((header) => {
            if (header.dataset.bound === "true") {
                return;
            }
            header.dataset.bound = "true";
            header.draggable = header.dataset.columnKey !== "actions";
            header.classList.add("table-header-cell");
            if (header.dataset.sortType !== "none") {
                header.addEventListener("click", () => this.sortByHeader(header));
            }
            header.addEventListener("dragstart", (event) => this.handleHeaderDragStart(event, header));
            header.addEventListener("dragover", (event) => this.handleHeaderDragOver(event, header));
            header.addEventListener("drop", (event) => this.handleHeaderDrop(event, header));
        });
    }

    handleHeaderDragStart(event, header) {
        this.draggedColumnKey = header.dataset.columnKey;
        event.dataTransfer.effectAllowed = "move";
    }

    handleHeaderDragOver(event) {
        event.preventDefault();
    }

    handleHeaderDrop(event, targetHeader) {
        event.preventDefault();
        if (!this.draggedColumnKey || this.draggedColumnKey === targetHeader.dataset.columnKey) {
            return;
        }
        const order = Array.from(this.tableElement.querySelectorAll("thead th")).map((header) => header.dataset.columnKey);
        const sourceIndex = order.indexOf(this.draggedColumnKey);
        const targetIndex = order.indexOf(targetHeader.dataset.columnKey);
        const [columnKey] = order.splice(sourceIndex, 1);
        order.splice(targetIndex, 0, columnKey);
        const state = this.readTableState();
        state.order = order;
        this.saveTableState(state);
        this.applyTableState();
    }

    sortByHeader(header) {
        const columnKey = header.dataset.columnKey;
        const sortType = header.dataset.sortType || "text";
        const currentDirection = header.dataset.sortDirection === "asc" ? "desc" : "asc";
        this.tableElement.querySelectorAll("thead th").forEach((item) => {
            item.dataset.sortDirection = "";
            item.classList.remove("sorted-asc", "sorted-desc");
        });
        header.dataset.sortDirection = currentDirection;
        header.classList.add(currentDirection === "asc" ? "sorted-asc" : "sorted-desc");

        const body = this.tableElement.querySelector("tbody");
        const rows = Array.from(body.querySelectorAll("tr"));
        rows.sort((first, second) => {
            const firstValue = first.querySelector(`[data-column-key="${columnKey}"]`)?.textContent.trim() || "";
            const secondValue = second.querySelector(`[data-column-key="${columnKey}"]`)?.textContent.trim() || "";
            return this.compareValues(firstValue, secondValue, sortType, currentDirection);
        });
        rows.forEach((row) => body.appendChild(row));
    }

    compareValues(firstValue, secondValue, sortType, direction) {
        let result = 0;
        if (sortType === "number") {
            result = Number(firstValue || 0) - Number(secondValue || 0);
        } else if (sortType === "date") {
            result = this.parseDateValue(firstValue) - this.parseDateValue(secondValue);
        } else {
            result = firstValue.localeCompare(secondValue, "ru", { sensitivity: "base" });
        }
        return direction === "asc" ? result : -result;
    }

    parseDateValue(value) {
        const [day, month, year] = value.split(".");
        return new Date(Number(year || 0), Number(month || 1) - 1, Number(day || 1)).getTime();
    }

    renderColumnManager() {
        if (!this.tableColumnsManager || !this.tableElement) {
            return;
        }
        const state = this.readTableState();
        const hidden = new Set(state.hidden);
        const headers = Array.from(this.tableElement.querySelectorAll("thead th"))
            .filter((header) => header.dataset.columnKey !== "actions");

        this.tableColumnsManager.innerHTML = headers.map((header) => `
            <div class="col-md-4">
                <label class="form-check table-column-option">
                    <input class="form-check-input table-column-toggle" type="checkbox" data-column-key="${header.dataset.columnKey}" ${hidden.has(header.dataset.columnKey) ? "" : "checked"}>
                    <span class="form-check-label">${header.textContent.trim()}</span>
                </label>
            </div>
        `).join("");

        this.tableColumnsManager.querySelectorAll(".table-column-toggle").forEach((input) => {
            input.addEventListener("change", () => {
                const nextHidden = Array.from(this.tableColumnsManager.querySelectorAll(".table-column-toggle"))
                    .filter((checkbox) => !checkbox.checked)
                    .map((checkbox) => checkbox.dataset.columnKey);
                const nextState = this.readTableState();
                nextState.hidden = nextHidden;
                this.saveTableState(nextState);
                this.applyTableState();
            });
        });
    }

    getVisibleExportColumns() {
        if (!this.tableElement) {
            return [];
        }
        return Array.from(this.tableElement.querySelectorAll("thead th"))
            .filter((header) => !header.classList.contains("table-column-hidden"))
            .filter((header) => header.dataset.exportable !== "false")
            .map((header) => ({
                key: header.dataset.columnKey,
                label: header.textContent.trim(),
            }));
    }

    exportCurrentTable() {
        if (!this.tableElement) {
            alert("Нет данных для выгрузки");
            return;
        }

        const columns = this.getVisibleExportColumns();
        const rows = Array.from(this.tableElement.querySelectorAll("tbody tr")).map((row) =>
            columns.map((column) => {
                const cell = row.querySelector(`[data-column-key="${column.key}"]`);
                return cell ? cell.textContent.trim() : "";
            })
        );

        const tableHtml = `
            <table>
                <thead>
                    <tr>${columns.map((column) => `<th>${this.escapeHtml(column.label)}</th>`).join("")}</tr>
                </thead>
                <tbody>
                    ${rows.map((row) => `<tr>${row.map((value) => `<td>${this.escapeHtml(value)}</td>`).join("")}</tr>`).join("")}
                </tbody>
            </table>
        `;
        const documentHtml = `
            <html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel">
                <head><meta charset="utf-8"></head>
                <body>${tableHtml}</body>
            </html>
        `;
        const blob = new Blob([documentHtml], { type: "application/vnd.ms-excel;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        const now = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 16);
        link.href = url;
        link.download = `competitions-${this.getCurrentView()}-${now}.xls`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
    }

    escapeHtml(value) {
        return value
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;");
    }

    setLoading(isActive) {
        if (!this.loader) {
            return;
        }
        this.loader.classList.toggle("loader-active", isActive);
    }
}

window.addEventListener("DOMContentLoaded", () => {
    new Main();
});

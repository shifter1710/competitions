class Main {
    constructor() {
        this.currentReportUrl = null;
        this.draggedColumnKey = null;
        this.isAthlete = document.body.dataset.role === "athlete";
        this.initializeDomReferences();
        this.createInstances();
        this.hangEvents();
        this.initTableFeatures();
        this.applyProfileToManualForm();
    }

    initializeDomReferences() {
        this.contentWrapper = document.querySelector(".content-wrapper");
        this.importForm = document.querySelector(".import-form");
        this.manualForm = document.querySelector(".manual-form");
        this.manualFormButton = document.querySelector(".manual-form__button");
        this.manualFormCancelButton = document.querySelector(".manual-form__cancel-button");
        this.manualDateInput = document.querySelector('.manual-form [name="date"]');
        this.customFieldInputs = Array.from(document.querySelectorAll(".custom-field-input"));
        this.profileForm = document.querySelector(".profile-form");
        this.profileFormStatus = document.querySelector(".profile-form__status");
        this.profileNameHint = document.querySelector(".profile-form__name-hint");
        this.profileFormCancelButton = document.querySelector(".profile-form__cancel-button");
        this.addEmptyRowButton = document.querySelector(".add-empty-row-button");
        this.mergeForm = document.querySelector(".merge-form");
        this.mergeControls = document.querySelector(".merge-controls");
        this.mergeApplyButton = document.querySelector(".merge-apply-button");
        this.mergeStatus = document.querySelector(".merge-status");
        this.reportForm = document.querySelector(".filter-form");
        this.reportExportForm = document.querySelector(".report-export-form");
        this.fileInput = document.querySelector(".import-form__input");
        this.attachmentFileInput = document.querySelector(".attachment-file-input");
        this.importButton = document.querySelector(".import-form__button");
        this.loader = document.querySelector(".loader");
        this.inputName = document.querySelector(".filter__name");
        this.dateFromInput = document.querySelector(".filter__date-from");
        this.dateToInput = document.querySelector(".filter__date-to");
        this.levelSelectWrapper = document.querySelector(".filter__level-wrapper");
        this.positionSelectWrapper = document.querySelector(".filter__position-wrapper");
        this.cleanFilterButton = document.querySelector(".filter-form__clean-button");
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
        if (this.reportExportForm) {
            this.reportExportForm.addEventListener("submit", (event) => this.handleSubmitReportExportForm(event));
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
        if (this.addEmptyRowButton) {
            this.addEmptyRowButton.addEventListener("click", () => this.startNewRowEdit());
        }
        if (this.profileForm) {
            this.profileForm.addEventListener("submit", (event) => this.handleSubmitProfileForm(event));
            this.fetchProfile().then(() => {
                this.fillProfileForm();
                this.checkStudentNameHint(this.loadProfile().student_name);
            });
        } else if (this.isAthlete) {
            // На странице таблицы формы профиля нет, но профиль нужен для
            // автоподстановки в новые записи (форма и инлайн-строка).
            this.fetchProfile().then(() => this.applyProfileToManualForm());
        }
        if (this.profileFormCancelButton) {
            this.profileFormCancelButton.addEventListener("click", () => this.resetProfile());
        }
        if (this.mergeForm) {
            this.mergePreview = null;
            this.mergeForm.addEventListener("submit", (event) => this.handleMergePreview(event));
        }
        if (this.mergeApplyButton) {
            this.mergeApplyButton.addEventListener("click", () => this.handleMergeApply());
        }
        if (this.attachmentFileInput) {
            this.attachmentFileInput.addEventListener("change", () => this.handleAttachmentSelected());
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
        this.reportForm.querySelectorAll('[name^="custom__"]').forEach((input) => {
            input.value = "";
        });
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
        this.applyProfileToManualForm();
    }

    // Источник данных профиля — сервер (/api/profile). Профиль хранится
    // в базе и привязывает записи к аккаунту; localStorage больше не нужен.
    loadProfile() {
        return this.cachedProfile || {};
    }

    fetchProfile() {
        return fetch("/api/profile")
            .then((response) => (response.ok ? response.json() : {profile: {}}))
            .then((data) => {
                this.cachedProfile = data.profile || {};
                return this.cachedProfile;
            })
            .catch(() => {
                this.cachedProfile = {};
                return this.cachedProfile;
            });
    }

    saveProfile(profile) {
        this.cachedProfile = profile;
        const formData = new FormData();
        Object.entries(profile).forEach(([key, value]) => formData.append(key, value));
        formData.append("csrf_token", this.getCsrfToken());
        return fetch("/api/profile", {method: "POST", body: formData})
            .then((response) => {
                if (!response.ok) {
                    return response.text().then((message) => {
                        throw new Error(message || "Ошибка сохранения профиля");
                    });
                }
                return response.json();
            })
            .then((data) => {
                this.cachedProfile = data.profile || profile;
                return this.cachedProfile;
            });
    }

    clearProfile() {
        this.cachedProfile = {};
        return this.saveProfile({});
    }

    handleSubmitProfileForm(event) {
        event.preventDefault();
        const formData = new FormData(this.profileForm);
        const profile = {};
        ["student_name", "student_sex", "institute", "group", "course"].forEach((key) => {
            profile[key] = String(formData.get(key) || "").trim();
        });
        this.saveProfile(profile)
            .then(() => {
                if (this.profileFormStatus) {
                    this.profileFormStatus.textContent = "Профиль сохранён";
                }
                this.checkStudentNameHint(profile.student_name);
            })
            .catch((error) => {
                if (this.profileFormStatus) {
                    this.profileFormStatus.textContent = error.message;
                }
            });
    }

    fillProfileForm() {
        if (!this.profileForm) {
            return;
        }
        const profile = this.loadProfile();
        this.profileForm.querySelectorAll("input, select").forEach((input) => {
            const value = profile[input.name];
            if (value !== undefined) {
                input.value = value;
            }
        });
    }

    resetProfile() {
        if (!this.profileForm) {
            return;
        }
        this.clearProfile();
        this.profileForm.reset();
        if (this.profileFormStatus) {
            this.profileFormStatus.textContent = "";
        }
        this.checkStudentNameHint("");
    }

    // Тихая проверка ФИО атлета: Lookup возвращает только факт точного
    // совпадения, чужие ФИО атлету не раскрываются. При любой ошибке сети
    // подсказка не показывается.
    checkStudentNameHint(name) {
        if (!this.isAthlete || !this.profileNameHint) {
            return;
        }
        const trimmed = String(name || "").trim();
        if (!trimmed) {
            this.profileNameHint.textContent = "";
            this.profileNameHint.classList.remove("text-success", "text-warning");
            return;
        }
        fetch(`/api/students/lookup?name=${encodeURIComponent(trimmed)}`)
            .then((response) => (response.ok ? response.json() : Promise.reject(new Error("lookup failed"))))
            .then((data) => {
                if (data.found) {
                    this.profileNameHint.textContent =
                        "Найдены записи с таким ФИО — участия привяжутся к кабинету";
                    this.profileNameHint.className = "profile-form__name-hint form-text mt-1 text-success";
                } else {
                    this.profileNameHint.textContent =
                        "Записей с таким ФИО не найдено — проверьте написание (например, полное ФИО вместо инициалов)";
                    this.profileNameHint.className = "profile-form__name-hint form-text mt-1 text-warning";
                }
            })
            .catch(() => {
                this.profileNameHint.textContent = "";
                this.profileNameHint.classList.remove("text-success", "text-warning");
            });
    }

    // Подсказки ФИО в админке: datalist наполняется один раз за сессию,
    // повторно — после перерисовки контента. Список чужих ФИО доступен
    // только admin/editor; при 403 или ошибке сети поля просто без подсказок.
    renderStudentNamesList() {
        const datalist = document.querySelector("#student-names-list");
        if (!datalist) {
            return;
        }
        if (this.studentNames) {
            datalist.replaceChildren(...this.studentNames.map((name) => {
                const option = document.createElement("option");
                option.value = name;
                return option;
            }));
            return;
        }
        fetch("/api/students")
            .then((response) => (response.ok ? response.json() : Promise.reject(new Error("students list failed"))))
            .then((names) => {
                if (!Array.isArray(names)) {
                    return;
                }
                this.studentNames = names;
                this.renderStudentNamesList();
            })
            .catch(() => {});
    }

    // Автоподстановка из профиля (docs/data-model-decisions.md): профиль
    // подставляет значения в новые записи атлета, пока поле не заполнено
    // в форме; введённое вручную всегда приоритетнее.
    getProfileFieldDefaults() {
        if (!this.isAthlete) {
            return {};
        }
        const profile = this.loadProfile();
        const defaults = {};
        ["student_name", "student_sex", "institute", "group", "course"].forEach((key) => {
            const value = String(profile[key] ?? "").trim();
            if (value) {
                defaults[key] = value;
            }
        });
        return defaults;
    }

    applyProfileToManualForm() {
        if (!this.isAthlete || !this.manualForm) {
            return;
        }
        Object.entries(this.getProfileFieldDefaults()).forEach(([key, value]) => {
            const input = this.manualForm.querySelector(`[name="${key}"]`);
            if (input && !input.value) {
                input.value = value;
            }
        });
    }

    // Формирует form-encoded тело для merge-запросов. CSRF-токен
    // прикрепляется в makeRequest ко всем POST (FormData, URLSearchParams
    // и запросам без тела), здесь дублирование исключено проверкой has().
    buildMergeRequestBody(extraFields = {}) {
        const body = new URLSearchParams(extraFields);
        body.append("csrf_token", this.getCsrfToken());
        return body;
    }

    setMergeStatus(message, tone = "muted") {
        if (!this.mergeStatus) {
            return;
        }
        this.mergeStatus.textContent = message;
        this.mergeStatus.className = `merge-status text-${tone}`;
    }

    resetMergeControls(message, tone) {
        this.mergePreview = null;
        if (this.mergeControls) {
            this.mergeControls.classList.add("d-none");
        }
        if (this.mergeApplyButton) {
            this.mergeApplyButton.disabled = true;
        }
        if (message !== undefined) {
            this.setMergeStatus(message, tone);
        }
    }

    handleMergePreview(event) {
        event.preventDefault();
        this.resetMergeControls();
        const fromName = this.mergeForm.querySelector('[name="from_name"]').value.trim();
        const toName = this.mergeForm.querySelector('[name="to_name"]').value.trim();
        if (!fromName || !toName) {
            this.setMergeStatus("Укажите оба ФИО", "danger");
            return;
        }

        this.makeRequest({
            url: "/admin/students/merge",
            options: {
                method: "POST",
                body: this.buildMergeRequestBody({from_name: fromName, to_name: toName}),
            },
            onSuccess: (responseBody) => {
                let preview;
                try {
                    preview = JSON.parse(responseBody);
                } catch {
                    this.setMergeStatus("Неожиданный ответ сервера", "danger");
                    return;
                }
                const count = preview.records_to_merge;
                if (!count) {
                    this.setMergeStatus("Записей с таким ФИО не найдено", "warning");
                    return;
                }
                this.mergePreview = {fromName, toName, count};
                this.setMergeStatus(`Найдено записей для объединения: ${count}`, "success");
                if (this.mergeControls) {
                    this.mergeControls.classList.remove("d-none");
                }
                if (this.mergeApplyButton) {
                    this.mergeApplyButton.disabled = false;
                }
            },
            onError: (message) => this.setMergeStatus(message || "Ошибка предпросмотра", "danger"),
        });
    }

    handleMergeApply() {
        if (!this.mergePreview || !this.mergeApplyButton || this.mergeApplyButton.disabled) {
            return;
        }
        const {fromName, toName, count} = this.mergePreview;
        const confirmed = confirm(
            `Объединить ${count} записей с «${fromName}» на «${toName}»? Действие необратимо`
        );
        if (!confirmed) {
            return;
        }

        const fields = {
            from_name: fromName,
            to_name: toName,
            confirm: "on",
        };
        const rewriteCheckbox = this.mergeForm.querySelector('[name="rewrite_names"]');
        if (rewriteCheckbox && rewriteCheckbox.checked) {
            fields.rewrite_names = "on";
        }

        this.mergeApplyButton.disabled = true;
        this.makeRequest({
            url: "/admin/students/merge",
            options: {
                method: "POST",
                body: this.buildMergeRequestBody(fields),
            },
            onSuccess: (responseBody) => {
                let result;
                try {
                    result = JSON.parse(responseBody);
                } catch {
                    result = {};
                }
                const merged = typeof result.merged === "number" ? result.merged : count;
                this.resetMergeControls(
                    `Объединено записей: ${merged}` +
                        (result.rewritten_names ? ". Отображаемые ФИО в записях переписаны" : ""),
                    "success"
                );
            },
            onError: (message) => {
                this.mergeApplyButton.disabled = false;
                this.setMergeStatus(message || "Ошибка объединения", "danger");
            },
        });
    }

    getTableLevels() {
        try {
            return JSON.parse(this.tableElement?.dataset.levels || "[]");
        } catch {
            return [];
        }
    }

    getCustomFieldTypes() {
        const types = {};
        this.customFieldInputs.forEach((input) => {
            types[input.dataset.customFieldKey] = input.dataset.customFieldType || "text";
        });
        return types;
    }

    createInlineInput(key, value, fieldTypes) {
        let input;
        if (key === "student_sex" || key === "level") {
            input = document.createElement("select");
            const options = key === "student_sex"
                ? ["М", "Ж"]
                : this.getTableLevels();
            options.forEach((optionValue) => {
                const option = document.createElement("option");
                option.value = optionValue;
                option.textContent = optionValue;
                input.append(option);
            });
            input.value = value;
        } else {
            input = document.createElement("input");
            const isCustom = Object.prototype.hasOwnProperty.call(fieldTypes, key);
            const fieldType = isCustom ? fieldTypes[key] : null;
            if (key === "position" || key === "course" || fieldType === "number") {
                input.type = "number";
                input.step = "1";
            } else if (fieldType === "url") {
                input.type = "url";
                input.placeholder = "https://";
            } else {
                input.type = "text";
            }
            if (key === "date" || fieldType === "date") {
                input.placeholder = "дд.мм.гггг";
            }
            // Подсказки справочников: datalist лежит в разметке страницы,
            // id совпадает с ключом поля (sport-options / institute-options).
            if (key === "sport" || key === "institute") {
                input.setAttribute("list", `${key}-options`);
            }
            input.value = value ?? "";
        }
        input.dataset.editKey = key;
        input.className = "form-control form-control-sm";
        return input;
    }

    startInlineEdit(button) {
        const row = button.closest("tr");
        if (!row) {
            return;
        }
        this.cancelInlineEdit();
        this.cancelNewRowEdit();

        const dataset = button.dataset;
        const extraData = dataset.extraJson ? JSON.parse(dataset.extraJson) : {};
        const fieldTypes = this.getCustomFieldTypes();
        const values = {
            student_name: dataset.studentName,
            student_sex: dataset.studentSex,
            institute: dataset.institute,
            group: dataset.group,
            sport: dataset.sport,
            date: dataset.date,
            level: dataset.level,
            name: dataset.name,
            position: dataset.position,
            course: dataset.course,
        };

        this.inlineEditRow = row;
        this.inlineEditRecordId = dataset.recordId;
        this.inlineEditBackup = row.innerHTML;
        row.classList.add("row-editing");

        Array.from(row.children).forEach((cell) => {
            const key = cell.dataset.columnKey;
            if (!key || key === "index") {
                return;
            }
            if (key === "actions") {
                cell.textContent = "";
                const saveButton = document.createElement("button");
                saveButton.type = "button";
                saveButton.className = "btn btn-sm btn-success inline-save-button";
                saveButton.textContent = "✓";
                saveButton.title = "Сохранить";
                const cancelButton = document.createElement("button");
                cancelButton.type = "button";
                cancelButton.className = "btn btn-sm btn-outline-secondary inline-cancel-button";
                cancelButton.textContent = "✗";
                cancelButton.title = "Отмена";
                cell.append(saveButton, cancelButton);
                return;
            }
            const value = Object.prototype.hasOwnProperty.call(values, key)
                ? values[key]
                : extraData[key] ?? "";
            cell.textContent = "";
            cell.append(this.createInlineInput(key, value, fieldTypes));
        });

        const dateInput = row.querySelector('[data-edit-key="date"]');
        if (dateInput) {
            this.inlineDatePicker = new Datepicker(dateInput, {
                autohide: true,
                format: "dd.mm.yyyy"
            });
        }
        this.inlineKeydownHandler = (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                this.cancelInlineEdit();
            } else if (event.key === "Enter" && event.target.tagName !== "SELECT") {
                event.preventDefault();
                this.saveInlineEdit();
            }
        };
        row.addEventListener("keydown", this.inlineKeydownHandler);
        const firstInput = row.querySelector("[data-edit-key]");
        if (firstInput) {
            firstInput.focus();
        }
    }

    saveInlineEdit() {
        if (!this.inlineEditRow || !this.inlineEditRecordId) {
            return;
        }
        const fieldTypes = this.getCustomFieldTypes();
        const formData = new FormData();
        this.inlineEditRow.querySelectorAll("[data-edit-key]").forEach((input) => {
            const key = input.dataset.editKey;
            const name = Object.prototype.hasOwnProperty.call(fieldTypes, key)
                ? `custom__${key}`
                : key;
            formData.append(name, input.value);
        });

        this.makeRequest({
            url: `/competition/${this.inlineEditRecordId}`,
            options: {
                method: "POST",
                body: formData,
            },
            onSuccess: () => {
                alert("Запись успешно обновлена");
                this.refreshCurrentContent();
            },
            onError: (message) => alert(message || "Ошибка сохранения записи")
        });
    }

    cancelInlineEdit() {
        if (!this.inlineEditRow) {
            return;
        }
        const row = this.inlineEditRow;
        if (this.inlineKeydownHandler) {
            row.removeEventListener("keydown", this.inlineKeydownHandler);
            this.inlineKeydownHandler = null;
        }
        if (this.inlineDatePicker) {
            this.inlineDatePicker.destroy();
            this.inlineDatePicker = null;
        }
        if (row.isConnected) {
            row.innerHTML = this.inlineEditBackup;
            row.classList.remove("row-editing");
        }
        this.inlineEditRow = null;
        this.inlineEditRecordId = null;
        this.inlineEditBackup = null;
    }

    hasActiveRowEdit() {
        return Boolean(this.inlineEditRow || this.newEditRow);
    }

    buildRowActionsCell(cell, saveClassName) {
        const saveButton = document.createElement("button");
        saveButton.type = "button";
        saveButton.className = `btn btn-sm btn-success ${saveClassName}`;
        saveButton.textContent = "✓";
        saveButton.title = "Сохранить";
        const cancelButton = document.createElement("button");
        cancelButton.type = "button";
        cancelButton.className = "btn btn-sm btn-outline-secondary new-row-cancel-button";
        cancelButton.textContent = "✗";
        cancelButton.title = "Отмена";
        cell.append(saveButton, cancelButton);
    }

    bindRowKeyboardNavigation(row, saveAction, cancelAction) {
        const handler = (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                cancelAction();
            } else if (event.key === "Enter" && event.target.tagName !== "SELECT") {
                event.preventDefault();
                saveAction();
            }
        };
        row.addEventListener("keydown", handler);
        return handler;
    }

    startNewRowEdit() {
        if (!this.tableElement) {
            alert("Таблица недоступна: нет данных");
            return;
        }
        if (this.hasActiveRowEdit()) {
            return;
        }
        const tbody = this.tableElement.querySelector("tbody");
        if (!tbody) {
            return;
        }

        const fieldTypes = this.getCustomFieldTypes();
        const profileDefaults = this.getProfileFieldDefaults();
        const row = document.createElement("tr");
        row.classList.add("row-editing", "row-new");

        Array.from(this.tableElement.querySelectorAll("thead th")).forEach((header) => {
            const key = header.dataset.columnKey;
            const cell = document.createElement("td");
            if (key) {
                cell.dataset.columnKey = key;
            }
            if (!key || key === "index" || key === "status" || key === "attachments") {
                row.append(cell);
                return;
            }
            if (key === "actions") {
                this.buildRowActionsCell(cell, "new-row-save-button");
                row.append(cell);
                return;
            }
            cell.append(this.createInlineInput(key, profileDefaults[key] ?? "", fieldTypes));
            row.append(cell);
        });

        // Новая строка открывается первой строкой таблицы, сразу после
        // заголовков, — до неё не нужно прокручивать сотню записей.
        tbody.prepend(row);
        this.newEditRow = row;

        const dateInput = row.querySelector('[data-edit-key="date"]');
        if (dateInput) {
            this.newRowDatePicker = new Datepicker(dateInput, {
                autohide: true,
                format: "dd.mm.yyyy"
            });
        }
        this.newRowKeydownHandler = this.bindRowKeyboardNavigation(
            row,
            () => this.saveNewRowEdit(),
            () => this.cancelNewRowEdit()
        );
        row.scrollIntoView({behavior: "smooth", block: "center"});
        // Фокус на первое незаполненное поле (при автоподстановке из профиля
        // заполненные пропускаем); preventScroll, чтобы не сбивать плавную
        // прокрутку к строке.
        const inputs = Array.from(row.querySelectorAll("[data-edit-key]"));
        (inputs.find((input) => !input.value) || inputs[0])?.focus({preventScroll: true});
    }

    saveNewRowEdit() {
        if (!this.newEditRow) {
            return;
        }
        const fieldTypes = this.getCustomFieldTypes();
        const formData = new FormData();
        this.newEditRow.querySelectorAll("[data-edit-key]").forEach((input) => {
            const key = input.dataset.editKey;
            const name = Object.prototype.hasOwnProperty.call(fieldTypes, key)
                ? `custom__${key}`
                : key;
            formData.append(name, input.value);
        });

        this.makeRequest({
            url: "/competition",
            options: {
                method: "POST",
                body: formData,
            },
            onSuccess: () => {
                alert("Запись успешно добавлена");
                this.refreshCurrentContent();
            },
            onError: (message) => alert(message || "Ошибка добавления записи")
        });
    }

    cancelNewRowEdit() {
        if (!this.newEditRow) {
            return;
        }
        const row = this.newEditRow;
        if (this.newRowKeydownHandler) {
            row.removeEventListener("keydown", this.newRowKeydownHandler);
            this.newRowKeydownHandler = null;
        }
        if (this.newRowDatePicker) {
            this.newRowDatePicker.destroy();
            this.newRowDatePicker = null;
        }
        row.remove();
        this.newEditRow = null;
    }

    uploadAttachment(recordId, file) {
        const formData = new FormData();
        formData.append("file", file);
        this.makeRequest({
            url: `/competition/${recordId}/attachments`,
            options: {method: "POST", body: formData},
            onSuccess: (body) => {
                alert(body || "Файл загружен");
                this.refreshCurrentContent();
            },
            onError: (message) => alert(message || "Ошибка загрузки файла")
        });
    }

    handleAttachmentSelected() {
        const input = this.attachmentFileInput;
        const file = input && input.files && input.files[0];
        const recordId = input && input.dataset.recordId;
        input.value = "";
        if (!file || !recordId) {
            return;
        }
        this.uploadAttachment(recordId, file);
    }

    deleteAttachment(attachmentId, filename) {
        const message = filename
            ? `Удалить вложение «${filename}»?`
            : "Удалить вложение?";
        if (!confirm(message)) {
            return;
        }
        this.makeRequest({
            url: `/attachment/${attachmentId}/delete`,
            options: {method: "POST"},
            onSuccess: () => this.refreshCurrentContent(),
            onError: (message2) => alert(message2 || "Ошибка удаления вложения")
        });
    }

    reviewCompetition(recordId, decision) {
        if (decision === "reject") {
            const comment = prompt("Комментарий для владельца записи (необязательно):") ?? "";
            this.makeRequest({
                url: `/competition/${recordId}/review/reject`,
                options: {
                    method: "POST",
                    body: new URLSearchParams({comment}),
                },
                onSuccess: () => this.refreshCurrentContent(),
                onError: (message) => alert(message || "Ошибка отклонения записи")
            });
            return;
        }
        this.makeRequest({
            url: `/competition/${recordId}/review/approve`,
            options: {method: "POST"},
            onSuccess: () => this.refreshCurrentContent(),
            onError: (message) => alert(message || "Ошибка подтверждения записи")
        });
    }

    handleContentWrapperClick(event) {
        const attachmentAddButton = event.target.closest(".attachment-add-button");
        if (attachmentAddButton && this.attachmentFileInput) {
            this.attachmentFileInput.dataset.recordId = attachmentAddButton.dataset.recordId;
            this.attachmentFileInput.click();
            return;
        }

        const attachmentDeleteButton = event.target.closest(".attachment-delete-button");
        if (attachmentDeleteButton) {
            this.deleteAttachment(
                attachmentDeleteButton.dataset.attachmentId,
                attachmentDeleteButton.dataset.filename
            );
            return;
        }

        const approveButton = event.target.closest(".competition-approve-button");
        if (approveButton) {
            this.reviewCompetition(approveButton.dataset.recordId, "approve");
            return;
        }

        const rejectButton = event.target.closest(".competition-reject-button");
        if (rejectButton) {
            this.reviewCompetition(rejectButton.dataset.recordId, "reject");
            return;
        }

        const newRowSaveButton = event.target.closest(".new-row-save-button");
        if (newRowSaveButton) {
            this.saveNewRowEdit();
            return;
        }

        const newRowCancelButton = event.target.closest(".new-row-cancel-button");
        if (newRowCancelButton) {
            this.cancelNewRowEdit();
            return;
        }

        const saveButton = event.target.closest(".inline-save-button");
        if (saveButton) {
            this.saveInlineEdit();
            return;
        }

        const cancelButton = event.target.closest(".inline-cancel-button");
        if (cancelButton) {
            this.cancelInlineEdit();
            return;
        }

        const editButton = event.target.closest(".competition-edit-button");
        if (editButton) {
            this.startInlineEdit(editButton);
            return;
        }

        const deleteButton = event.target.closest(".competition-delete-button");
        if (deleteButton) {
            this.deleteCompetition(deleteButton.dataset.recordId, deleteButton.dataset.studentName);
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

    // Выгрузка отчёта в Excel: колонки — из чекбоксов панели, фильтры —
    // текущие (применённые к показанной таблице, а до первого применения —
    // из полей формы фильтра). Всё уходит GET-параметрами, состояния на
    // сервере нет.
    handleSubmitReportExportForm(event) {
        event.preventDefault();
        const selectedColumns = Array.from(
            this.reportExportForm.querySelectorAll(".report-export-column:checked")
        ).map((input) => input.value);
        if (!selectedColumns.length) {
            alert("Выберите хотя бы одну колонку");
            return;
        }
        const filterParams = this.currentReportUrl
            ? this.currentReportUrl.split("?")[1] || ""
            : this.prepareParamsForReport();
        const params = new URLSearchParams(filterParams);
        selectedColumns.forEach((column) => params.append("columns", column));
        window.location.href = `/export/report?${params.toString()}`;
    }

    importFile(formData) {
        this.makeRequest({
            url: "/",
            options: {
                method: "POST",
                body: formData,
            },
            onSuccess: (body) => {
                alert(body || "Файл успешно импортирован");
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

    deleteCompetition(recordId, studentName) {
        const message = studentName
            ? `Удалить запись «${studentName}»?`
            : "Удалить запись?";
        const result = confirm(message);
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

    getCsrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : "";
    }

    makeRequest({ url, options = {}, onSuccess = () => {}, onError = () => {} }) {
        const csrfToken = this.getCsrfToken();
        if (options.body instanceof FormData) {
            if (!options.body.has("csrf_token")) {
                options.body.append("csrf_token", csrfToken);
            }
        } else if (options.body instanceof URLSearchParams) {
            if (!options.body.has("csrf_token")) {
                options.body.append("csrf_token", csrfToken);
            }
        } else if (!options.body && String(options.method || "").toUpperCase() === "POST") {
            options.body = new URLSearchParams({csrf_token: csrfToken});
        }
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
            .then((body) => {
                this.setLoading(false);
                onSuccess(body);
            })
            .catch((error) => {
                this.setLoading(false);
                if (error.message === "Redirected to login" || error.message === "Unauthorized") {
                    return;
                }
                if (error.message && error.message.includes("CSRF")) {
                    alert("Сессия обновлена в другой вкладке. Обновите страницу (F5) и повторите действие");
                    return;
                }
                onError(error.message);
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
        this.renderStudentNamesList();
        this.inlineEditRow = null;
        this.inlineEditRecordId = null;
        this.inlineEditBackup = null;
        this.newEditRow = null;
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
        if (this.hasActiveRowEdit()) {
            return;
        }
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
        if (this.hasActiveRowEdit()) {
            return;
        }
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

        this.tableColumnsManager.replaceChildren(...headers.map((header) => {
            const wrapper = document.createElement("div");
            wrapper.className = "col-md-4";
            const label = document.createElement("label");
            label.className = "form-check table-column-option";
            const input = document.createElement("input");
            input.className = "form-check-input table-column-toggle";
            input.type = "checkbox";
            input.dataset.columnKey = header.dataset.columnKey;
            input.checked = !hidden.has(header.dataset.columnKey);
            const span = document.createElement("span");
            span.className = "form-check-label";
            span.textContent = header.textContent.trim();
            label.append(input, span);
            wrapper.append(label);
            return wrapper;
        }));

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
        this.cancelInlineEdit();
        this.cancelNewRowEdit();

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

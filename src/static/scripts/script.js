// Комбобокс инлайн-строки (№4): input + кнопка-стрелка + выпадающий список.
// Ввод с клавиатуры фильтрует список по подстроке, значение можно ввести
// свободно — подсказка, не валидация. Список живёт в <body> с position:
// fixed: таблица лежит в .table-responsive (overflow: auto), абсолютное
// позиционирование внутри ячейки обрезалось бы контейнером.
class InlineComboBox {
    constructor(input, toggleButton, getOptions) {
        this.input = input;
        this.toggleButton = toggleButton;
        this.getOptions = getOptions;
        this.open = false;
        this.highlightIndex = -1;
        this.closeTimer = null;
        this.dropdown = document.createElement("ul");
        this.dropdown.className = "combobox-dropdown d-none";
        document.body.appendChild(this.dropdown);
        this.handleOutsideMousedown = (event) => {
            if (
                this.open &&
                event.target !== this.input &&
                !this.toggleButton.contains(event.target) &&
                !this.dropdown.contains(event.target)
            ) {
                this.close();
            }
        };
        this.reposition = () => this.positionDropdown();
        document.addEventListener("mousedown", this.handleOutsideMousedown);
        input.addEventListener("input", (event) => {
            // Синтетический input после выбора из списка не должен открывать
            // список заново — он нужен только связке институт→группа.
            if (!event.isTrusted) {
                return;
            }
            this.openDropdown();
        });
        input.addEventListener("keydown", (event) => this.handleKeydown(event));
        input.addEventListener("blur", () => {
            this.closeTimer = setTimeout(() => this.close(), 120);
        });
        toggleButton.addEventListener("click", (event) => {
            event.preventDefault();
            clearTimeout(this.closeTimer);
            if (this.open) {
                this.close();
            } else {
                this.openDropdown();
            }
            this.input.focus();
        });
        this.dropdown.addEventListener("mousedown", (event) => {
            const option = event.target.closest(".combobox-option");
            if (!option) {
                return;
            }
            event.preventDefault(); // фокус остаётся в поле до выбора значения
            this.select(option.dataset.value);
        });
    }

    isOpen() {
        return this.open;
    }

    openDropdown() {
        this.renderOptions();
        this.open = true;
        this.dropdown.classList.remove("d-none");
        this.positionDropdown();
        window.addEventListener("scroll", this.reposition, true);
        window.addEventListener("resize", this.reposition);
    }

    close() {
        this.open = false;
        this.highlightIndex = -1;
        this.dropdown.classList.add("d-none");
        window.removeEventListener("scroll", this.reposition, true);
        window.removeEventListener("resize", this.reposition);
    }

    refresh() {
        if (!this.open) {
            return;
        }
        this.renderOptions();
        this.positionDropdown();
    }

    filteredOptions() {
        const query = this.input.value.trim().toLowerCase();
        const options = this.getOptions() || [];
        if (!query) {
            return options;
        }
        return options.filter((option) => option.toLowerCase().includes(query));
    }

    renderOptions() {
        const options = this.filteredOptions();
        if (!options.length) {
            const empty = document.createElement("li");
            empty.className = "combobox-empty";
            empty.textContent = "Нет совпадений — можно ввести своё значение";
            this.dropdown.replaceChildren(empty);
            this.highlightIndex = -1;
            return;
        }
        this.highlightIndex = Math.min(this.highlightIndex, options.length - 1);
        this.dropdown.replaceChildren(
            ...options.map((value) => {
                const item = document.createElement("li");
                item.className = "combobox-option";
                item.dataset.value = value;
                item.textContent = value;
                item.setAttribute("role", "option");
                return item;
            })
        );
        this.updateHighlight();
    }

    visibleOptions() {
        return Array.from(this.dropdown.querySelectorAll(".combobox-option"));
    }

    updateHighlight() {
        const options = this.visibleOptions();
        options.forEach((item, index) => {
            item.classList.toggle("combobox-option-active", index === this.highlightIndex);
        });
        const active = options[this.highlightIndex];
        if (active) {
            active.scrollIntoView({block: "nearest"});
        }
    }

    positionDropdown() {
        if (!this.open) {
            return;
        }
        const rect = this.input.getBoundingClientRect();
        this.dropdown.style.left = `${rect.left}px`;
        this.dropdown.style.top = `${rect.bottom + 2}px`;
        this.dropdown.style.minWidth = `${rect.width}px`;
    }

    handleKeydown(event) {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            event.stopPropagation();
            if (!this.open) {
                this.openDropdown();
                return;
            }
            const options = this.visibleOptions();
            if (!options.length) {
                return;
            }
            const direction = event.key === "ArrowDown" ? 1 : -1;
            this.highlightIndex = (this.highlightIndex + direction + options.length) % options.length;
            this.updateHighlight();
            return;
        }
        if (event.key === "Enter" && this.open) {
            // Открытый список перехватывает Enter: выбор подсвеченного (или
            // просто закрытие) — сохранение строки не срабатывает.
            event.preventDefault();
            event.stopPropagation();
            if (this.highlightIndex >= 0) {
                this.select(this.visibleOptions()[this.highlightIndex].dataset.value);
            } else {
                this.close();
            }
            return;
        }
        if (event.key === "Escape" && this.open) {
            // Escape сначала закрывает список; повторный — отменяет правку.
            event.preventDefault();
            event.stopPropagation();
            this.close();
        }
    }

    // Строковый обработчик Enter/Escape (capture-фаза на <tr>) спрашивает
    // виджет, открыт ли список: открытое меню работает в два шага.
    isOpen() {
        return this.open;
    }

    select(value) {
        this.input.value = value;
        this.close();
        // Выбор из списка — тоже изменение значения: связка институт→группа
        // слушает input и перезаполнит список групп.
        this.input.dispatchEvent(new Event("input", {bubbles: true}));
    }

    destroy() {
        clearTimeout(this.closeTimer);
        this.close();
        document.removeEventListener("mousedown", this.handleOutsideMousedown);
        this.dropdown.remove();
    }
}

// Резолвер атлета (№23доп, docs/feedback-live.md): тихий поиск по ФИО в
// инлайн-строке главной. Ввод ≥2 символов — запрос /api/athletes/search
// (троттлинг + отмена, ошибки сети тихие), выбор варианта подставляет
// известные sex/institute/group/course в строку; поля остаются
// редактируемыми — автоподстановка, не принуждение. Свободный ввод ФИО
// без выбора — как раньше. Список живёт в <body> с position: fixed, как у
// InlineComboBox: таблица в .table-responsive обрезала бы абсолют.
// Вызовы только у ролей с правом записи: атлету чужие ФИО не раскрываются
// (эндпоинт отвечает 403, его автоподстановка из профиля уже работает).
class FioResolver {
    constructor(input, onSelect) {
        this.input = input;
        this.onSelect = onSelect;
        this.open = false;
        this.highlightIndex = -1;
        this.closeTimer = null;
        this.throttleTimer = null;
        this.abortController = null;
        this.athletes = [];
        this.dropdown = document.createElement("ul");
        this.dropdown.className = "combobox-dropdown d-none";
        document.body.appendChild(this.dropdown);
        this.handleOutsideMousedown = (event) => {
            if (
                this.open &&
                event.target !== this.input &&
                !this.dropdown.contains(event.target)
            ) {
                this.close();
            }
        };
        this.reposition = () => this.positionDropdown();
        document.addEventListener("mousedown", this.handleOutsideMousedown);
        input.addEventListener("input", () => this.scheduleSearch());
        input.addEventListener("keydown", (event) => this.handleKeydown(event));
        input.addEventListener("blur", () => {
            this.closeTimer = setTimeout(() => this.close(), 120);
        });
        this.dropdown.addEventListener("mousedown", (event) => {
            const option = event.target.closest(".combobox-option");
            if (!option) {
                return;
            }
            event.preventDefault(); // фокус остаётся в поле до выбора варианта
            const athlete = this.athletes.find(
                (item) => String(item.name) === option.dataset.name
            );
            if (athlete) {
                this.select(athlete);
            }
        });
    }

    scheduleSearch() {
        clearTimeout(this.throttleTimer);
        const query = this.input.value.trim();
        if (query.length < 2) {
            this.abortRequest();
            this.close();
            return;
        }
        this.throttleTimer = setTimeout(() => this.search(query), 250);
    }

    abortRequest() {
        if (this.abortController) {
            this.abortController.abort();
            this.abortController = null;
        }
    }

    async search(query) {
        this.abortRequest();
        const controller = new AbortController();
        this.abortController = controller;
        try {
            const response = await fetch(
                `/api/athletes/search?q=${encodeURIComponent(query)}`,
                {signal: controller.signal}
            );
            if (!response.ok) {
                this.close();
                return;
            }
            const athletes = await response.json();
            if (controller.signal.aborted) {
                return;
            }
            this.athletes = Array.isArray(athletes) ? athletes : [];
            if (this.athletes.length) {
                this.openDropdown();
            } else {
                this.close();
            }
        } catch (error) {
            // Тихо: ошибка сети или отмена — поле ФИО остаётся свободным вводом.
            if (!controller.signal.aborted) {
                this.close();
            }
        }
    }

    isOpen() {
        return this.open;
    }

    openDropdown() {
        this.renderOptions();
        this.open = true;
        this.dropdown.classList.remove("d-none");
        this.positionDropdown();
        window.addEventListener("scroll", this.reposition, true);
        window.addEventListener("resize", this.reposition);
    }

    close() {
        this.open = false;
        this.highlightIndex = -1;
        this.dropdown.classList.add("d-none");
        window.removeEventListener("scroll", this.reposition, true);
        window.removeEventListener("resize", this.reposition);
    }

    renderOptions() {
        this.highlightIndex = -1;
        this.dropdown.replaceChildren(
            ...this.athletes.map((athlete) => {
                const item = document.createElement("li");
                item.className = "combobox-option";
                item.dataset.name = athlete.name;
                item.textContent = athlete.name;
                item.title = [
                    athlete.sex,
                    athlete.institute,
                    athlete.group,
                    athlete.course ? `курс ${athlete.course}` : ""
                ].filter(Boolean).join(", ");
                item.setAttribute("role", "option");
                return item;
            })
        );
    }

    visibleOptions() {
        return Array.from(this.dropdown.querySelectorAll(".combobox-option"));
    }

    updateHighlight() {
        const options = this.visibleOptions();
        options.forEach((item, index) => {
            item.classList.toggle("combobox-option-active", index === this.highlightIndex);
        });
        const active = options[this.highlightIndex];
        if (active) {
            active.scrollIntoView({block: "nearest"});
        }
    }

    positionDropdown() {
        if (!this.open) {
            return;
        }
        const rect = this.input.getBoundingClientRect();
        this.dropdown.style.left = `${rect.left}px`;
        this.dropdown.style.top = `${rect.bottom + 2}px`;
        this.dropdown.style.minWidth = `${rect.width}px`;
    }

    handleKeydown(event) {
        if (!this.open) {
            return;
        }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            event.stopPropagation();
            const options = this.visibleOptions();
            if (!options.length) {
                return;
            }
            const direction = event.key === "ArrowDown" ? 1 : -1;
            this.highlightIndex = (this.highlightIndex + direction + options.length) % options.length;
            this.updateHighlight();
            return;
        }
        if (event.key === "Enter") {
            // Открытый список перехватывает Enter: выбор подсвеченного или
            // закрытие — сохранение строки не срабатывает.
            event.preventDefault();
            event.stopPropagation();
            if (this.highlightIndex >= 0) {
                const option = this.visibleOptions()[this.highlightIndex];
                const athlete = this.athletes.find((item) => String(item.name) === option.dataset.name);
                if (athlete) {
                    this.select(athlete);
                    return;
                }
            }
            this.close();
            return;
        }
        if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            this.close();
        }
    }

    select(athlete) {
        this.input.value = athlete.name;
        this.close();
        this.onSelect(athlete);
    }

    // См. InlineComboBox.isOpen: строковый обработчик Enter/Escape в
    // capture-фазе не трогает строку, пока открыт список резолвера.
    isOpen() {
        return this.open;
    }

    destroy() {
        clearTimeout(this.closeTimer);
        clearTimeout(this.throttleTimer);
        this.abortRequest();
        this.close();
        document.removeEventListener("mousedown", this.handleOutsideMousedown);
        this.dropdown.remove();
    }
}

class Main {
    constructor() {
        this.currentReportUrl = null;
        this.draggedColumnKey = null;
        this.isAthlete = document.body.dataset.role === "athlete";
        this.activeComboBoxes = [];
        this.activeFioResolvers = [];
        // Защита от двойного сабмита инлайн-правки/новой строки (см. save*Edit).
        this.inlineSaving = false;
        this.newRowSaving = false;
        this.initializeDomReferences();
        this.createInstances();
        this.hangEvents();
        this.initTableFeatures();
    }

    initializeDomReferences() {
        this.contentWrapper = document.querySelector(".content-wrapper");
        this.importForm = document.querySelector(".import-form");
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
        this.themeToggleButton = document.querySelector(".theme-toggle-button");
        this.hintsToggleButton = document.querySelector(".hints-toggle-button");
        this.usersSearchInput = document.querySelector(".users-search-input");
        this.usersRoleFilter = document.querySelector(".users-role-filter");
        this.usersTable = document.querySelector(".users-table");
        this.filterIsApplied = Boolean(this.tableCard && this.tableCard.dataset.tableView === "report");
    }

    createInstances() {
        if (this.dateRangePickerElement) {
            this.dateRangePicker = new DateRangePicker(this.dateRangePickerElement, {
                format: "dd.mm.yyyy"
            });
        }
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
            // автоподстановки в инлайн-строку новой записи (замечание №1:
            // отдельная форма ввода убрана, ввод — только «+ Пустая строка»).
            this.fetchProfile();
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
        if (this.themeToggleButton) {
            this.updateThemeToggleButton();
            this.themeToggleButton.addEventListener("click", () => this.handleThemeToggle());
        }
        if (this.hintsToggleButton) {
            this.updateHintsToggleButton();
            this.hintsToggleButton.addEventListener("click", () => this.handleHintsToggle());
        }
        this.initUsersPage();
        this.initIndexFilterCard();
        this.bindContentWrapperEvents();
    }

    // Страница «Пользователи» (/admin/users): живой поиск по логину и фильтр
    // по роли — чистый клиент над уже загруженной таблицей, без запросов к
    // серверу. Модалы действий (пароль/ФИО/удаление) наполняются из
    // data-атрибутов кнопки строки.
    initUsersPage() {
        if (!this.usersTable) {
            return;
        }
        if (this.usersSearchInput) {
            this.usersSearchInput.addEventListener("input", () => this.filterUsersRows());
        }
        if (this.usersRoleFilter) {
            this.usersRoleFilter.addEventListener("change", () => this.filterUsersRows());
        }
        document.addEventListener("click", (event) => this.handleUsersPageClick(event));
        const deleteConfirmInput = document.querySelector(".user-delete-confirm");
        if (deleteConfirmInput) {
            deleteConfirmInput.addEventListener("input", () => this.validateUserDeleteConfirm());
        }
    }

    filterUsersRows() {
        const query = String((this.usersSearchInput && this.usersSearchInput.value) || "").trim().toLowerCase();
        const role = this.usersRoleFilter ? this.usersRoleFilter.value : "";
        this.usersTable.querySelectorAll("tbody tr").forEach((row) => {
            const matchesQuery = !query || (row.dataset.username || "").toLowerCase().includes(query);
            const matchesRole = !role || row.dataset.role === role;
            row.classList.toggle("d-none", !(matchesQuery && matchesRole));
        });
    }

    // Карточка фильтров главной (прототип 02): применение и сброс —
    // GET-навигация, фильтры живут в URL (шарость ссылок). «Показывать по»
    // меняет per_page и сбрасывает на первую страницу. Карточка лежит вне
    // content-wrapper и не переинициализируется при обновлении таблицы.
    initIndexFilterCard() {
        const filterCard = document.querySelector(".index-filter-card");
        if (filterCard && filterCard.dataset.bound !== "true") {
            filterCard.dataset.bound = "true";
            const form = filterCard.querySelector("form");
            if (form) {
                form.addEventListener("submit", (event) => {
                    event.preventDefault();
                    const params = new URLSearchParams();
                    new FormData(form).forEach((value, key) => {
                        const text = String(value).trim();
                        if (text) {
                            params.append(key, text);
                        }
                    });
                    const query = params.toString();
                    window.location.href = query ? `/?${query}` : "/";
                });
            }
            const resetLink = filterCard.querySelector(".index-filter-reset");
            if (resetLink) {
                resetLink.addEventListener("click", (event) => {
                    event.preventDefault();
                    window.location.href = "/";
                });
            }
            this.initHybridDateInputs(filterCard);
        }
        const perPageSelect = document.querySelector(".per-page-select");
        if (perPageSelect && perPageSelect.dataset.bound !== "true") {
            perPageSelect.dataset.bound = "true";
            perPageSelect.addEventListener("change", () => {
                const params = new URLSearchParams(perPageSelect.dataset.query || "");
                params.set("per_page", perPageSelect.value);
                window.location.href = `/?${params.toString()}`;
            });
        }
    }

    // Гибридные даты (замечание №13) вне инлайн-строки: поля «Дата от/до»
    // карточки фильтров главной. Ручной ввод цифрами с авто-точками плюс
    // datepicker по кнопке-календарю — как в строках таблицы.
    initHybridDateInputs(root) {
        root.querySelectorAll(".index-date-group").forEach((group) => {
            const input = group.querySelector('input[type="text"]');
            const toggleButton = group.querySelector(".index-date-toggle");
            if (!input || input.dataset.dateBound === "true") {
                return;
            }
            input.dataset.dateBound = "true";
            input.addEventListener("input", (event) => this.handleManualDateInput(event));
            input.addEventListener("blur", () => this.normalizeDateInput(input));
            const picker = new Datepicker(input, {autohide: true, format: "dd.mm.yyyy"});
            if (toggleButton) {
                toggleButton.addEventListener("click", (event) => {
                    event.preventDefault();
                    picker.show();
                });
            }
        });
    }

    handleUsersPageClick(event) {
        const passwordButton = event.target.closest(".user-password-button");
        if (passwordButton) {
            this.prepareUserModal(".user-password-form", "/password", passwordButton);
            return;
        }
        const aliasButton = event.target.closest(".user-alias-button");
        if (aliasButton) {
            this.prepareUserModal(".user-alias-form", "/alias", aliasButton);
            return;
        }
        const deleteButton = event.target.closest(".user-delete-button");
        if (deleteButton) {
            this.prepareUserModal(".user-delete-form", "/delete", deleteButton);
            const confirmInput = document.querySelector(".user-delete-confirm");
            if (confirmInput) {
                confirmInput.value = "";
            }
            this.validateUserDeleteConfirm();
            return;
        }
        const generateButton = event.target.closest(".password-generate-button");
        if (generateButton) {
            const input = document.querySelector(generateButton.dataset.passwordInput);
            if (input) {
                input.value = this.generatePassword();
                input.focus();
            }
            return;
        }
        const copyButton = event.target.closest(".password-copy-button");
        if (copyButton) {
            const input = document.querySelector(copyButton.dataset.passwordInput);
            if (input && input.value) {
                this.copyPasswordToClipboard(input.value, copyButton);
            }
        }
    }

    prepareUserModal(formSelector, actionSuffix, button) {
        const form = document.querySelector(formSelector);
        if (!form) {
            return;
        }
        form.action = `/admin/users/${button.dataset.userId}${actionSuffix}`;
        form.dataset.username = button.dataset.username || "";
        form.querySelectorAll(".user-modal-username").forEach((element) => {
            element.textContent = form.dataset.username;
        });
        const recordsElement = form.querySelector(".user-delete-modal-records-count");
        if (recordsElement) {
            recordsElement.textContent = button.dataset.recordsCount || "0";
        }
    }

    // Кнопка «Удалить» в модале активна только при точном совпадении логина.
    validateUserDeleteConfirm() {
        const form = document.querySelector(".user-delete-form");
        if (!form) {
            return;
        }
        const confirmInput = form.querySelector(".user-delete-confirm");
        const submitButton = form.querySelector(".user-delete-submit");
        const hint = form.querySelector(".user-delete-confirm-hint");
        if (!confirmInput || !submitButton) {
            return;
        }
        const matches = confirmInput.value === form.dataset.username;
        submitButton.disabled = !matches;
        if (hint) {
            hint.classList.toggle("d-none", !confirmInput.value || matches);
        }
    }

    // Криптослучайный пароль: 16 символов из алфавита без двусмысленных
    // (исключены 0, O, 1, l, I). Байты из «хвоста» диапазона отбрасываются,
    // чтобы распределение символов было равномерным без смещения по модулю.
    generatePassword(length = 16) {
        const alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
        if (!window.crypto || !crypto.getRandomValues) {
            alert("Браузер не поддерживает безопасную генерацию паролей");
            return "";
        }
        const maxValidByte = 256 - (256 % alphabet.length);
        const bytes = new Uint8Array(length * 2);
        let password = "";
        while (password.length < length) {
            crypto.getRandomValues(bytes);
            for (const byte of bytes) {
                if (byte >= maxValidByte) {
                    continue;
                }
                password += alphabet[byte % alphabet.length];
                if (password.length === length) {
                    break;
                }
            }
        }
        return password;
    }

    // Копирование пароля: navigator.clipboard, при недоступности —
    // fallback через скрытую textarea и execCommand.
    copyPasswordToClipboard(value, button) {
        const flashCopied = () => {
            const originalLabel = button.textContent;
            button.textContent = "Скопировано";
            setTimeout(() => {
                button.textContent = originalLabel;
            }, 1500);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard
                .writeText(value)
                .then(flashCopied)
                .catch(() => this.copyViaExecCommand(value, flashCopied));
            return;
        }
        this.copyViaExecCommand(value, flashCopied);
    }

    copyViaExecCommand(value, successCallback) {
        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        let copied = false;
        try {
            copied = document.execCommand("copy");
        } catch {
            copied = false;
        }
        document.body.removeChild(textarea);
        if (copied) {
            successCallback();
        } else {
            alert("Не удалось скопировать пароль — скопируйте вручную");
        }
    }

    // Тема живёт только на клиенте: атрибут data-bs-theme на <html> ставит
    // inline-скрипт в base.html до отрисовки, здесь — переключение и запись
    // выбора в localStorage (ключ "theme": light|dark). Дефолт — светлая.
    handleThemeToggle() {
        const nextTheme = document.documentElement.dataset.bsTheme === "dark" ? "light" : "dark";
        document.documentElement.dataset.bsTheme = nextTheme;
        localStorage.setItem("theme", nextTheme);
        this.updateThemeToggleButton();
    }

    updateThemeToggleButton() {
        if (!this.themeToggleButton) {
            return;
        }
        const isDark = document.documentElement.dataset.bsTheme === "dark";
        this.themeToggleButton.textContent = isDark ? "☀️" : "🌙";
        this.themeToggleButton.title = isDark ? "Включить светлую тему" : "Включить тёмную тему";
    }

    // Подсказки (№26) живут только на клиенте: класс hints-off на <html>
    // ставит inline-скрипт в base.html до отрисовки, здесь — переключение
    // класса и запись выбора в localStorage (ключ "hints": on|off).
    // Дефолт — включены. Скрытие элементов .ui-hint делает main.css.
    handleHintsToggle() {
        const hintsOff = document.documentElement.classList.toggle("hints-off");
        try {
            localStorage.setItem("hints", hintsOff ? "off" : "on");
        } catch (error) {
            // localStorage недоступен — подсказки переключаются до перезагрузки
        }
        this.updateHintsToggleButton();
    }

    updateHintsToggleButton() {
        if (!this.hintsToggleButton) {
            return;
        }
        const hintsOff = document.documentElement.classList.contains("hints-off");
        this.hintsToggleButton.textContent = hintsOff ? "✕💡" : "💡";
        this.hintsToggleButton.title = hintsOff ? "Включить подсказки" : "Скрыть подсказки";
        this.hintsToggleButton.setAttribute("aria-label", hintsOff ? "Показывать подсказки" : "Скрывать подсказки");
    }

    bindContentWrapperEvents() {
        if (!this.contentWrapper || this.contentWrapper.dataset.bound === "true") {
            return;
        }
        this.contentWrapper.dataset.bound = "true";
        this.contentWrapper.addEventListener("click", (event) => this.handleContentWrapperClick(event));
        this.contentWrapper.addEventListener("dblclick", (event) => this.handleContentWrapperDblClick(event));
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

    // Автоформат даты при ручном вводе (замечание №13): только цифры,
    // точки «дд.мм.гггг» проставляются сами — буквы и точки не набираются.
    // Даты-диапазоны (№4): в поле «Дата» строки можно набрать «25-27.06.2026»,
    // «30.01-01.02.2026» или полный «25.06.2026-27.06.2026» — при наличии
    // дефиса авто-точки выключаются, пропускаем цифры, точки и дефис как есть.
    handleManualDateInput(event) {
        const input = event.target;
        if (input.dataset.editKey === "date" && input.value.includes("-")) {
            input.value = input.value.replace(/[^\d.-]/g, "");
            return;
        }
        const digits = input.value.replace(/\D/g, "").slice(0, 8);
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

        input.value = parts.join(".");
    }

    normalizeDateInput(input) {
        // Диапазон («25-27.06.2026» и т.п.) не нормализуем — его форму
        // проверяет сервер.
        if (input.dataset.editKey === "date" && input.value.includes("-")) {
            return;
        }
        const digits = input.value.replace(/\D/g, "").slice(0, 8);
        if (digits.length <= 4) {
            return;
        }

        const day = digits.slice(0, 2);
        const month = digits.slice(2, 4);
        const year = digits.slice(4, 8);
        input.value = [day, month, year].filter(Boolean).join(".");
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
    // подставляет значения в новые записи атлета, пока поле не заполнено;
    // введённое вручную всегда приоритетнее. Используется инлайн-строкой
    // «+ Пустая строка».
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

    // Типы кастомных полей (замечание №1): раньше читались из инпутов
    // удалённой формы ввода, теперь — из data-field-type заголовков колонок
    // таблицы (index.html). Нужны инлайн-строке: тип поля ввода и префикс
    // custom__<key> при сохранении.
    getCustomFieldTypes() {
        const types = {};
        if (!this.tableElement) {
            return types;
        }
        this.tableElement.querySelectorAll("thead th[data-field-type]").forEach((header) => {
            types[header.dataset.columnKey] = header.dataset.fieldType || "text";
        });
        return types;
    }

    // Источники данных комбобоксов (№4/№15): институты — активный datalist
    // страницы, группы — карта институт → группы на таблице (только активные).
    getInstituteOptions() {
        const datalist = document.querySelector("#institute-options");
        if (!datalist) {
            return [];
        }
        return Array.from(datalist.querySelectorAll("option"))
            .map((option) => option.value)
            .filter(Boolean);
    }

    getGroupOptionsByInstitute() {
        try {
            return JSON.parse(this.tableElement?.dataset.groupsByInstitute || "{}");
        } catch {
            return {};
        }
    }

    getAllGroupOptions() {
        const map = this.getGroupOptionsByInstitute();
        return Array.from(new Set(Object.values(map).flat()))
            .sort((first, second) => first.localeCompare(second, "ru"));
    }

    getGroupOptionsForInstitute(instituteValue) {
        const map = this.getGroupOptionsByInstitute();
        const key = String(instituteValue || "").trim();
        if (key && Object.prototype.hasOwnProperty.call(map, key)) {
            return map[key].slice();
        }
        // Институт пуст или ещё не из справочника — подсказываем все группы
        // (свободный ввод остаётся, значение в поле не стирается).
        return this.getAllGroupOptions();
    }

    getComboboxOptions(key, input) {
        if (key === "institute") {
            return this.getInstituteOptions();
        }
        const row = input.closest("tr");
        const instituteInput = row ? row.querySelector('[data-edit-key="institute"]') : null;
        return this.getGroupOptionsForInstitute(instituteInput ? instituteInput.value : "");
    }

    destroyRowComboBoxes() {
        this.activeComboBoxes.forEach((combo) => combo.destroy());
        this.activeComboBoxes = [];
    }

    // Резолвер атлета (№23доп): навешивается на поле ФИО инлайн-строки у
    // ролей с правом записи (атлету — нет: чужие ФИО не запрашиваем, его
    // автоподстановка из собственного профиля уже работает).
    attachFioResolver(row) {
        if (this.isAthlete) {
            return;
        }
        const input = row.querySelector('[data-edit-key="student_name"]');
        if (!input || input.type !== "text") {
            return;
        }
        this.activeFioResolvers.push(new FioResolver(input, (athlete) => {
            this.applyAthleteDefaults(row, athlete);
        }));
    }

    destroyFioResolvers() {
        this.activeFioResolvers.forEach((resolver) => resolver.destroy());
        this.activeFioResolvers = [];
    }

    // Выбор варианта подставляет известные поля в строку и оставляет их
    // редактируемыми: автоподстановка, не принуждение (историчность —
    // docs/data-model-decisions.md). Синтетический input обновляет список
    // групп при подстановке института.
    applyAthleteDefaults(row, athlete) {
        const mapping = {sex: "student_sex", institute: "institute", group: "group", course: "course"};
        Object.entries(mapping).forEach(([athleteKey, editKey]) => {
            const value = athlete[athleteKey];
            if (!value) {
                return;
            }
            const input = row.querySelector(`[data-edit-key="${editKey}"]`);
            if (input && String(input.value) !== String(value)) {
                input.value = value;
                input.dispatchEvent(new Event("input", {bubbles: true}));
            }
        });
    }

    // Связка институт→группа (№15): смена института перезаполняет открытый
    // список групп; введённая группа сохраняется как есть.
    linkRowCombos(row) {
        const instituteInput = row.querySelector('[data-edit-key="institute"]');
        const groupInput = row.querySelector('[data-edit-key="group"]');
        if (!instituteInput || !groupInput) {
            return;
        }
        instituteInput.addEventListener("input", () => {
            const combo = this.activeComboBoxes.find((item) => item.input === groupInput);
            if (combo) {
                combo.refresh();
            }
        });
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
                input.inputMode = "numeric";
                if (key === "date") {
                    input.title = "Диапазон: 25-27.06.2026 или 25.06.2026-27.06.2026";
                }
            }
            // Подсказки справочников: datalist лежит в разметке страницы,
            // id совпадает с ключом поля. Институт и группа — комбобоксы
            // (№4): список групп фильтруется выбранным институтом (№15).
            if (key === "sport") {
                input.setAttribute("list", "sport-options");
            }
            input.value = value ?? "";
        }
        input.dataset.editKey = key;
        input.className = "form-control form-control-sm";
        // Комбобокс (№4): поле + кнопка-стрелка + выпадающий список с
        // фильтрацией по подстроке; свободный ввод не ограничен.
        if (input.type === "text" && (key === "institute" || key === "group")) {
            const wrapper = document.createElement("div");
            wrapper.className = "input-group inline-combobox-group";
            const toggleButton = document.createElement("button");
            toggleButton.type = "button";
            toggleButton.className = "btn btn-outline-secondary btn-sm combobox-toggle";
            toggleButton.textContent = "▾";
            toggleButton.title = "Показать список";
            toggleButton.setAttribute("aria-label", "Показать список");
            wrapper.append(input, toggleButton);
            this.activeComboBoxes.push(
                new InlineComboBox(input, toggleButton, () => this.getComboboxOptions(key, input))
            );
            return wrapper;
        }
        // Гибридный ввод даты (замечание №13): поле + иконка-календарь,
        // открывающая datepicker; сам пикер и авто-точки вешаются
        // attachRowDatePickers при сборке строки.
        if (input.type === "text" && (key === "date" || fieldTypes[key] === "date")) {
            const wrapper = document.createElement("div");
            wrapper.className = "input-group inline-date-group";
            const toggleButton = document.createElement("button");
            toggleButton.type = "button";
            toggleButton.className = "btn btn-outline-secondary btn-sm inline-date-toggle";
            toggleButton.textContent = "📅";
            toggleButton.title = "Выбрать дату из календаря";
            toggleButton.setAttribute("aria-label", "Выбрать дату из календаря");
            wrapper.append(input, toggleButton);
            return wrapper;
        }
        return input;
    }

    // Поля дат в инлайн-строке (замечание №13): ручной ввод цифрами с
    // авто-точками (handleManualDateInput) + datepicker (fengyuanchen, тот
    // же, что в фильтрах отчёта) с кнопкой-календарем. Выбор из календаря
    // проставляет дату в формате дд.мм.гггг. Возвращает созданные пикеры —
    // их нужно разрушить при отмене правки (иначе «висячие» слушатели).
    attachRowDatePickers(row) {
        const fieldTypes = this.getCustomFieldTypes();
        const pickers = [];
        row.querySelectorAll("[data-edit-key]").forEach((input) => {
            const key = input.dataset.editKey;
            if (key !== "date" && fieldTypes[key] !== "date") {
                return;
            }
            input.addEventListener("input", (event) => this.handleManualDateInput(event));
            input.addEventListener("blur", () => this.normalizeDateInput(input));
            const picker = new Datepicker(input, {
                autohide: true,
                format: "dd.mm.yyyy"
            });
            const toggleButton = input.closest(".inline-date-group")?.querySelector(".inline-date-toggle");
            if (toggleButton) {
                toggleButton.addEventListener("click", (event) => {
                    event.preventDefault();
                    picker.show();
                });
            }
            pickers.push(picker);
        });
        return pickers;
    }

    destroyRowDatePickers(pickers) {
        (pickers || []).forEach((picker) => picker.destroy());
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

        this.inlineDatePickers = this.attachRowDatePickers(row);
        this.linkRowCombos(row);
        this.attachFioResolver(row);
        this.inlineKeydownHandler = (event) => {
            if (this.hasOpenRowDropdown()) {
                return;
            }
            if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                this.cancelInlineEdit();
            } else if (event.key === "Enter" && event.target.tagName !== "SELECT") {
                event.preventDefault();
                event.stopPropagation();
                this.saveInlineEdit();
            }
        };
        // Capture-фаза: обработчик строки срабатывает ДО виджетов поля
        // (datepicker перехватывает Esc, комбобоксы/резолвер — Enter/Esc в
        // target-фазе), иначе Enter/Escape до строки не доходили вовсе.
        row.addEventListener("keydown", this.inlineKeydownHandler, true);
        const firstInput = row.querySelector("[data-edit-key]");
        if (firstInput) {
            firstInput.focus();
        }
    }

    saveInlineEdit() {
        if (!this.inlineEditRow || !this.inlineEditRecordId || this.inlineSaving) {
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

        // Защита от повторного Enter и двойного клика по «✓»: один запрос
        // на одно сохранение, флаг снимается в onSuccess/onError.
        this.inlineSaving = true;
        this.makeRequest({
            url: `/competition/${this.inlineEditRecordId}`,
            options: {
                method: "POST",
                body: formData,
            },
            onSuccess: () => {
                this.inlineSaving = false;
                alert("Запись успешно обновлена");
                this.refreshCurrentContent();
            },
            onError: (message) => {
                this.inlineSaving = false;
                alert(message || "Ошибка сохранения записи");
            }
        });
    }

    cancelInlineEdit() {
        if (!this.inlineEditRow) {
            return;
        }
        const row = this.inlineEditRow;
        if (this.inlineKeydownHandler) {
            row.removeEventListener("keydown", this.inlineKeydownHandler, true);
            this.inlineKeydownHandler = null;
        }
        this.destroyRowDatePickers(this.inlineDatePickers);
        this.inlineDatePickers = null;
        this.destroyRowComboBoxes();
        this.destroyFioResolvers();
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

    // Открытый выпадающий список (комбобокс или резолвер ФИО) работает в два
    // шага: Enter выбирает вариант, Esc закрывает список. Пока список открыт,
    // строковый обработчик Enter/Escape строку не сохраняет и не отменяет.
    hasOpenRowDropdown() {
        return (
            this.activeComboBoxes.some((combo) => combo.isOpen()) ||
            this.activeFioResolvers.some((resolver) => resolver.isOpen())
        );
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
            if (this.hasOpenRowDropdown()) {
                return;
            }
            if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                cancelAction();
            } else if (event.key === "Enter" && event.target.tagName !== "SELECT") {
                event.preventDefault();
                event.stopPropagation();
                saveAction();
            }
        };
        // Capture-фаза — та же причина, что у инлайн-правки (см. startInlineEdit).
        row.addEventListener("keydown", handler, true);
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
            // №24 (hotfix): колонка привязанного link-поля скрыта через
            // d-none на th — ячейка инлайн-строки должна скрываться так же,
            // иначе лишнее видимое поле ломает вёрстку строки.
            if (header.classList.contains("d-none")) {
                cell.classList.add("d-none");
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

        this.newRowDatePickers = this.attachRowDatePickers(row);
        this.linkRowCombos(row);
        this.attachFioResolver(row);
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
        if (!this.newEditRow || this.newRowSaving) {
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

        // Защита от повторного Enter и двойного клика по «✓» (см. saveInlineEdit).
        this.newRowSaving = true;
        this.makeRequest({
            url: "/competition",
            options: {
                method: "POST",
                body: formData,
            },
            onSuccess: () => {
                this.newRowSaving = false;
                alert("Запись успешно добавлена");
                this.refreshCurrentContent();
            },
            onError: (message) => {
                this.newRowSaving = false;
                alert(message || "Ошибка добавления записи");
            }
        });
    }

    cancelNewRowEdit() {
        if (!this.newEditRow) {
            return;
        }
        const row = this.newEditRow;
        if (this.newRowKeydownHandler) {
            row.removeEventListener("keydown", this.newRowKeydownHandler, true);
            this.newRowKeydownHandler = null;
        }
        this.destroyRowDatePickers(this.newRowDatePickers);
        this.newRowDatePickers = null;
        this.destroyRowComboBoxes();
        this.destroyFioResolvers();
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
        const chipRemove = event.target.closest(".filter-chip__x");
        if (chipRemove) {
            this.removeReportFilter(chipRemove.dataset.removeKey);
            return;
        }

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

    // Двойной клик по строке — вход в правку (замечание №9): кнопка
    // «Редактировать» закреплена в колонке действий, жест остаётся
    // ускорителем. Во время активной правки игнорируется — двойной клик
    // в полях ввода выделяет слово и не должен перезапускать правку.
    handleContentWrapperDblClick(event) {
        if (this.hasActiveRowEdit()) {
            return;
        }
        const row = event.target.closest("tr");
        if (!row || row.classList.contains("row-editing") || row.classList.contains("row-new")) {
            return;
        }
        const editButton = row.querySelector(".competition-edit-button");
        if (editButton) {
            this.startInlineEdit(editButton);
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
        // Срез выгрузки — текущее значение селекта «Группировать по»
        // (замечание №19): панель колонок экспорта всегда соответствует ему.
        const reportSliceSelect = document.querySelector(".filter__group-by");
        if (reportSliceSelect && reportSliceSelect.value) {
            params.set("group_by", reportSliceSelect.value);
        }
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
                this.refreshCurrentContent();
            },
            onError: (message2) => alert(message2 || "Ошибка удаления записи")
        });
    }

    // Сброс одного условия отчёта (прототип 06): крестик на чипе перечитывает
    // отчёт без этого GET-параметра, остальные условия остаются.
    removeReportFilter(removeKey) {
        if (!removeKey) {
            return;
        }
        const params = new URLSearchParams(
            this.currentReportUrl ? this.currentReportUrl.split("?")[1] || "" : ""
        );
        params.delete(removeKey);
        this.getReport(params.toString());
    }

    getReport(params) {
        const url = "/report" + (params ? `?${params}` : "");
        this.setLoading(true);
        fetch(url, { method: "GET" })            .then((response) => {
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
        // Реестр живёт в URL (фильтры, page/per_page — прототип 02): после
        // правок/создания/удаления перечитываем текущий адрес, а не «/»,
        // чтобы применённые условия и страница не терялись.
        this.setLoading(true);
        const currentUrl = window.location.pathname + window.location.search;
        fetch(currentUrl, {method: "GET"})
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
        // Футер таблицы («Показывать по») заменяется вместе с content-wrapper —
        // переинициализация нужна после каждой перерисовки списка; карточка
        // фильтров у себя внутри защищена повторной привязке флагом.
        this.initIndexFilterCard();
        this.tableCard = document.querySelector(".table-card");
        this.tableElement = document.querySelector(".interactive-table");
        this.renderStudentNamesList();
        this.inlineEditRow = null;
        this.inlineEditRecordId = null;
        this.inlineEditBackup = null;
        this.newEditRow = null;
        // При замене контента правка сбрасывается вместе со строкой — пикеры
        // и комбобоксы надо разрушить явно: их слушатели на document/body
        // переживают заменённый DOM.
        this.destroyRowDatePickers(this.inlineDatePickers);
        this.inlineDatePickers = null;
        this.destroyRowDatePickers(this.newRowDatePickers);
        this.newRowDatePickers = null;
        this.destroyRowComboBoxes();
        this.destroyFioResolvers();
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
        // Колонка «Действия» закреплена (sticky right) и должна оставаться
        // последней — иначе закреплённая ячейка перекроет соседнюю колонку.
        if (!this.draggedColumnKey
            || this.draggedColumnKey === targetHeader.dataset.columnKey
            || targetHeader.dataset.columnKey === "actions") {
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
            // data-sort-value (если задан) — машинное значение ячейки:
            // «Дата» отображается компактным диапазоном («25-27.06.2026»),
            // сортируется по ISO-дате начала.
            const firstCell = first.querySelector(`[data-column-key="${columnKey}"]`);
            const secondCell = second.querySelector(`[data-column-key="${columnKey}"]`);
            const firstValue = firstCell?.dataset.sortValue || firstCell?.textContent.trim() || "";
            const secondValue = secondCell?.dataset.sortValue || secondCell?.textContent.trim() || "";
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
    initCalendarPeriodPickers();
    // Страница участников соревнования (волна B, прототип 16): резолвер ФИО
    // в строке добавления + инлайн-правка строки (место «дописать позже»).
    if (document.querySelector("[data-participants-page]")) {
        new ParticipantsPage();
    }
});

// Период календаря (calendar.html / calendar_event.html): единый всплывающий
// календарь RangePicker (отзыв владельца: «выбор диапазона 2-мя нажатиями на
// 1 календарь, в стиле главной таблицы»). Видимое readonly-поле показывает
// «DD.MM.YYYY» или «DD.MM.YYYY – DD.MM.YYYY», на submit в скрытое поле date
// уходит то же значение через дефис («DD.MM.YYYY-DD.MM.YYYY») — контракт
// POST /calendar/new и /calendar/<id>/edit не меняется, серверная валидация
// остаётся источником истины.
//
// Выбор: 1-й клик = начало, 2-й клик = конец (hover подсвечивает отрезок).
// Повторный клик по той же дате = однодневный диапазон и закрытие панели,
// поэтому одинарный режим отдельного переключателя не требует. Закрытие
// по Esc/клику вне панели при выбранном только начале фиксирует его
// как однодневную дату (прощение, а не отмена: пустое «по» = однодневное).

const RP_MONTH_NAMES = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
];
const RP_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];

function rpIsoToParts(iso) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || "").trim());
    if (!match) {
        return null;
    }
    return { y: Number(match[1]), m: Number(match[2]) - 1, d: Number(match[3]) };
}

function rpToTime(parts) {
    // Полдень локального дня: сравнения и сдвиги без сюрпризов DST/полуночи.
    return new Date(parts.y, parts.m, parts.d, 12).getTime();
}

function initCalendarPeriodPickers() {
    document.querySelectorAll("input[data-range-picker]").forEach((input) => {
        if (input.dataset.rpBound === "true") {
            return;
        }
        input.dataset.rpBound = "true";
        new RangePicker(input);
    });
}

class RangePicker {
    constructor(input) {
        this.input = input;
        const form = input.closest("form");
        this.hidden = form ? form.querySelector('input[name="date"][data-period-field]') : null;
        this.start = null;
        this.end = null;
        this.panel = null;
        this.onDocClick = null;
        this.onDocKey = null;
        const from = rpIsoToParts(input.dataset.rangeFrom);
        const to = rpIsoToParts(input.dataset.rangeTo);
        if (from) {
            this.start = from;
            this.end = to && rpToTime(to) > rpToTime(from) ? to : null;
        }
        this.input.addEventListener("click", () => this.open());
        // readonly-поле не участвует в constraint-валидации (required на нём
        // браузер игнорирует), поэтому пустой сабмит перехватываем сами:
        // вместо отправки открываем пикер.
        if (form) {
            form.addEventListener("submit", (event) => {
                if (!this.start) {
                    event.preventDefault();
                    this.open();
                    this.input.focus();
                }
            });
        }
        this.input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
                event.preventDefault();
                this.toggle();
            } else if (event.key === "Escape") {
                this.close();
            }
        });
        this.syncValue();
    }

    toggle() {
        if (this.panel) {
            this.close();
        } else {
            this.open();
        }
    }

    open() {
        if (this.panel) {
            return;
        }
        const anchor = this.start || rpIsoToParts(
            new Date().toLocaleDateString("sv-SE") // локальная today как YYYY-MM-DD
        );
        this.viewYear = anchor.y;
        this.viewMonth = anchor.m;
        this.panel = document.createElement("div");
        this.panel.className = "range-picker-panel";
        this.panel.setAttribute("role", "dialog");
        this.panel.setAttribute("aria-label", "Выбор периода");
        document.body.appendChild(this.panel);
        this.render();
        this.position();
        this.onDocClick = (event) => {
            if (!this.panel.contains(event.target) && event.target !== this.input) {
                this.close();
            }
        };
        this.onDocKey = (event) => {
            if (event.key === "Escape") {
                this.close();
            }
        };
        document.addEventListener("mousedown", this.onDocClick, true);
        document.addEventListener("keydown", this.onDocKey, true);
    }

    close() {
        if (!this.panel) {
            return;
        }
        document.removeEventListener("mousedown", this.onDocClick, true);
        document.removeEventListener("keydown", this.onDocKey, true);
        this.onDocClick = null;
        this.onDocKey = null;
        this.panel.remove();
        this.panel = null;
        this.syncValue();
    }

    position() {
        const rect = this.input.getBoundingClientRect();
        const width = this.panel.offsetWidth;
        const height = this.panel.offsetHeight;
        let left = rect.left;
        if (left + width > window.innerWidth - 8) {
            left = Math.max(8, window.innerWidth - width - 8);
        }
        let top = rect.bottom + 4;
        if (top + height > window.innerHeight - 8 && rect.top - height - 4 >= 8) {
            top = rect.top - height - 4;
        }
        this.panel.style.left = `${Math.round(left)}px`;
        this.panel.style.top = `${Math.round(top)}px`;
    }

    render() {
        const first = new Date(this.viewYear, this.viewMonth, 1, 12);
        // Сдвиг к понедельнику (неделя с пн, как в остальной таблице).
        let lead = (first.getDay() + 6) % 7;
        const cells = [];
        const cursor = new Date(first);
        cursor.setDate(cursor.getDate() - lead);
        const todayTime = rpToTime(rpIsoToParts(new Date().toLocaleDateString("sv-SE")));
        const startTime = this.start ? rpToTime(this.start) : null;
        const endTime = this.end ? rpToTime(this.end) : null;
        for (let i = 0; i < 42; i += 1) {
            const parts = { y: cursor.getFullYear(), m: cursor.getMonth(), d: cursor.getDate() };
            const time = rpToTime(parts);
            const outside = parts.m !== this.viewMonth;
            let state = "";
            if (outside) {
                state = " is-outside";
            }
            if (time === todayTime) {
                state += " is-today";
            }
            const inRange = startTime !== null && endTime !== null
                && time > startTime && time < endTime;
            if (inRange) {
                state += " is-in-range";
            }
            if ((startTime !== null && time === startTime)
                || (endTime !== null && time === endTime)) {
                state += " is-edge";
                if (startTime !== null && time === startTime
                    && endTime !== null && time === endTime) {
                    state += " is-single";
                }
            }
            cells.push(`<button type="button" class="rp-day${state}" data-day="${parts.y}-`
                + `${String(parts.m + 1).padStart(2, "0")}-${String(parts.d).padStart(2, "0")}"`
                + `${outside ? " tabindex=\"-1\"" : ""}>${parts.d}</button>`);
            cursor.setDate(cursor.getDate() + 1);
        }
        const dows = RP_WEEKDAYS.map((d) => `<span class="rp-dow">${d}</span>`).join("");
        this.panel.innerHTML = `
            <div class="rp-head">
                <button type="button" class="rp-nav" data-nav="-1" aria-label="Предыдущий месяц">&#8249;</button>
                <span class="rp-title">${RP_MONTH_NAMES[this.viewMonth]} ${this.viewYear}</span>
                <button type="button" class="rp-nav" data-nav="1" aria-label="Следующий месяц">&#8250;</button>
            </div>
            <div class="rp-grid">${dows}${cells.join("")}</div>
            <div class="rp-hint">Два клика: начало и конец. Повторный клик по той же дате — однодневное.</div>`;
        this.panel.querySelectorAll("[data-nav]").forEach((btn) => {
            btn.addEventListener("click", () => {
                this.viewMonth += Number(btn.dataset.nav);
                if (this.viewMonth < 0) {
                    this.viewMonth = 11;
                    this.viewYear -= 1;
                } else if (this.viewMonth > 11) {
                    this.viewMonth = 0;
                    this.viewYear += 1;
                }
                this.render();
                this.position();
            });
        });
        this.panel.querySelectorAll(".rp-day").forEach((btn) => {
            btn.addEventListener("mouseenter", () => this.previewHover(btn));
            btn.addEventListener("click", () => this.pickDay(btn.dataset.day));
        });
    }

    previewHover(btn) {
        // Hover-подсветка отрезка между первым кликом и текущей ячейкой.
        const day = rpIsoToParts(btn.dataset.day);
        if (!day || !this.start || this.end) {
            return;
        }
        const startTime = rpToTime(this.start);
        const dayTime = rpToTime(day);
        this.panel.querySelectorAll(".rp-day").forEach((cell) => {
            const parts = rpIsoToParts(cell.dataset.day);
            const time = rpToTime(parts);
            const between = dayTime > startTime
                ? time > startTime && time < dayTime
                : time < startTime && time > dayTime;
            cell.classList.toggle("is-in-range", between);
            cell.classList.toggle("is-edge-preview", time === dayTime);
        });
    }

    pickDay(iso) {
        const day = rpIsoToParts(iso);
        if (!day) {
            return;
        }
        if (!this.start || (this.start && this.end)) {
            // Новый выбор (или перезапуск, если кликнули раньше начала).
            this.start = day;
            this.end = null;
            this.render();
            return;
        }
        if (rpToTime(day) < rpToTime(this.start)) {
            // Конец раньше начала — начинаем выбор заново с этой даты.
            this.start = day;
            this.end = null;
            this.render();
            return;
        }
        this.end = rpToTime(day) === rpToTime(this.start) ? null : day;
        this.syncValue();
        this.close();
    }

    syncValue() {
        const fmt = (p) => `${String(p.d).padStart(2, "0")}.${String(p.m + 1).padStart(2, "0")}.${p.y}`;
        let display = "";
        let composed = "";
        if (this.start) {
            display = fmt(this.start);
            composed = display;
            if (this.end) {
                display += ` – ${fmt(this.end)}`;
                composed += `-${fmt(this.end)}`;
            }
        }
        this.input.value = display;
        if (this.hidden) {
            this.hidden.value = composed;
        }
    }
}

// Привязка пикеров периода к формам календаря (планирование, инлайн-правки,
// правка на /calendar/<id>): поле ввода readonly + скрытое поле date.

// Страница участников соревнования (волна B, docs/feedback-live.md №23).
// Переиспользует FioResolver (выбор варианта подставляет пол/институт/
// группу/курс — поля остаются редактируемыми) и те же эндпоинты записей
// реестра, что и инлайн-правка главной: сохранение — POST /competition/<id>,
// удаление — POST /competition/<id>/delete. Название/даты/уровень/спорт
// в правке не редактируются — берутся из data-* атрибутов таблицы (пресет
// события), историчность записи не нарушается.
class ParticipantsPage {
    constructor() {
        this.table = document.querySelector("[data-participants-page]");
        this.editingRow = null;
        this.editingBackup = null;
        this.keydownHandler = null;
        this.activeFioResolver = null;
        // Защита от двойного сабмита правки участника (см. saveEdit).
        this.participantSaving = false;
        this.attachAddFormResolver();
        this.table.addEventListener("click", (event) => this.handleClick(event));
    }

    getCsrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : "";
    }

    request(url, {body, onSuccess = () => {}, onError = () => {}}) {
        if (body instanceof FormData && !body.has("csrf_token")) {
            body.append("csrf_token", this.getCsrfToken());
        }
        fetch(url, {method: "POST", body})
            .then((response) => {
                if (!response.ok) {
                    return response.text().then((message) => {
                        throw new Error(message);
                    });
                }
                return response.text();
            })
            .then(onSuccess)
            .catch((error) => onError(error.message));
    }

    // Резолвер ФИО в строке добавления (та же логика, что у инлайн-строки
    // главной): выбор варианта подставляет известные значения, поля
    // остаются редактируемыми. Свободный ввод — как на главной.
    attachAddFormResolver() {
        const fioInput = document.getElementById("participant-fio");
        if (!fioInput || document.body.dataset.role === "athlete") {
            return;
        }
        const fill = (athlete, athleteKey, inputId) => {
            const value = athlete[athleteKey];
            const input = value ? document.getElementById(inputId) : null;
            if (input && !input.value) {
                input.value = value;
            }
        };
        new FioResolver(fioInput, (athlete) => {
            fill(athlete, "sex", "participant-sex");
            fill(athlete, "institute", "participant-institute");
            fill(athlete, "group", "participant-group");
            fill(athlete, "course", "participant-course");
        });
    }

    handleClick(event) {
        const editButton = event.target.closest(".participant-edit-button");
        if (editButton) {
            this.startEdit(editButton);
            return;
        }
        const saveButton = event.target.closest(".participant-save-button");
        if (saveButton) {
            this.saveEdit();
            return;
        }
        const cancelButton = event.target.closest(".participant-cancel-button");
        if (cancelButton) {
            this.cancelEdit();
            return;
        }
        const deleteButton = event.target.closest(".participant-delete-button");
        if (deleteButton) {
            this.deleteParticipant(deleteButton.dataset.recordId, deleteButton.dataset.studentName);
        }
    }

    buildTextInput(key, value, type = "text") {
        const input = document.createElement("input");
        input.type = type;
        input.className = "form-control form-control-sm";
        input.dataset.editKey = key;
        input.value = value ?? "";
        if (type === "number") {
            input.min = key === "course" ? "1" : "0";
            input.step = "1";
        }
        return input;
    }

    // Инлайн-правка строки участника: те же поля, что и при добавлении.
    // Место «дописать позже» — правка и сохранение записи целиком тем же
    // эндпоинтом, что и на главной (историчность — на сервере).
    startEdit(button) {
        if (this.editingRow) {
            return;
        }
        const row = button.closest("tr");
        const cells = row.querySelectorAll("td");
        const dataset = button.dataset;
        // Ячейки: №, ФИО, Пол, Институт, Группа, Курс, Место, Результат?, Действия
        const sexCell = cells[2];
        const instituteCell = cells[3];
        const groupCell = cells[4];
        const courseCell = cells[5];
        const positionCell = cells[6];
        const actionsCell = cells[cells.length - 1];

        this.editingBackup = row.innerHTML;
        this.editingRow = row;

        const fioInput = this.buildTextInput("student_name", dataset.studentName);
        const sexInput = document.createElement("select");
        ["М", "Ж"].forEach((option) => {
            const item = document.createElement("option");
            item.value = option;
            item.textContent = option;
            sexInput.append(item);
        });
        sexInput.className = "form-control form-control-sm";
        sexInput.dataset.editKey = "student_sex";
        sexInput.value = dataset.studentSex || "М";

        const courseInput = this.buildTextInput("course", dataset.course, "number");
        const positionInput = this.buildTextInput("position", dataset.position, "number");
        const instituteInput = this.buildTextInput("institute", dataset.institute);
        const groupInput = this.buildTextInput("group", dataset.group);

        instituteCell.replaceChildren(instituteInput);
        groupCell.replaceChildren(groupInput);
        sexCell.replaceChildren(sexInput);
        courseCell.replaceChildren(courseInput);
        positionCell.replaceChildren(positionInput);
        row.children[1].replaceChildren(fioInput);

        actionsCell.innerHTML = "";
        const saveButton = document.createElement("button");
        saveButton.type = "button";
        saveButton.className = "btn btn-sm btn-success participant-save-button";
        saveButton.textContent = "✓";
        saveButton.title = "Сохранить";
        const cancelButton = document.createElement("button");
        cancelButton.type = "button";
        cancelButton.className = "btn btn-sm btn-outline-secondary participant-cancel-button";
        cancelButton.textContent = "✗";
        cancelButton.title = "Отмена";
        actionsCell.append(saveButton, cancelButton);

        this.activeFioResolver = new FioResolver(fioInput, (athlete) => {
            const mapping = {sex: sexInput, institute: instituteInput, group: groupInput, course: courseInput};
            Object.entries(mapping).forEach(([athleteKey, input]) => {
                const value = athlete[athleteKey];
                if (value && String(input.value) !== String(value)) {
                    input.value = value;
                }
            });
        });

        this.keydownHandler = (keyEvent) => {
            // Открытый список резолвера работает в два шага (Enter — выбор,
            // Esc — закрытие): строку в это время не сохраняем и не отменяем.
            if (this.activeFioResolver && this.activeFioResolver.isOpen()) {
                return;
            }
            if (keyEvent.key === "Escape") {
                keyEvent.preventDefault();
                keyEvent.stopPropagation();
                this.cancelEdit();
            } else if (keyEvent.key === "Enter" && keyEvent.target.tagName !== "SELECT") {
                keyEvent.preventDefault();
                keyEvent.stopPropagation();
                this.saveEdit();
            }
        };
        // Capture-фаза: обработчик строки срабатывает раньше резолвера ФИО
        // (иначе его Enter/Esc в target-фазе съедали клавиши строки).
        row.addEventListener("keydown", this.keydownHandler, true);
        fioInput.focus();
    }

    saveEdit() {
        if (!this.editingRow || this.participantSaving) {
            return;
        }
        const recordId = this.editingRow.dataset.recordId;
        const formData = new FormData();
        this.editingRow.querySelectorAll("[data-edit-key]").forEach((input) => {
            formData.append(input.dataset.editKey, input.value);
        });
        // Поля события — из пресета таблицы: в записи они не редактируются.
        ["date", "level", "name", "sport"].forEach((key) => {
            formData.append(key, this.table.dataset[`event${key.charAt(0).toUpperCase()}${key.slice(1)}`] || "");
        });
        // Защита от повторного Enter и двойного клика по «✓» (тот же паттерн,
        // что у saveInlineEdit/saveNewRowEdit): один запрос на одно сохранение.
        this.participantSaving = true;
        this.request(`/competition/${recordId}`, {
            body: formData,
            onSuccess: () => {
                this.participantSaving = false;
                window.location.reload();
            },
            onError: (message) => {
                this.participantSaving = false;
                alert(message || "Ошибка сохранения записи");
            }
        });
    }

    cancelEdit() {
        if (!this.editingRow) {
            return;
        }
        if (this.keydownHandler) {
            this.editingRow.removeEventListener("keydown", this.keydownHandler, true);
            this.keydownHandler = null;
        }
        if (this.activeFioResolver) {
            this.activeFioResolver.destroy();
            this.activeFioResolver = null;
        }
        this.editingRow.innerHTML = this.editingBackup;
        this.editingRow = null;
        this.editingBackup = null;
    }

    // Удаление участника из соревнования — это обычное удаление записи
    // реестра (с правами роли: эндпоинт админский, кнопка видна только
    // админу). После удаления страница перечитается, счётчики обновятся.
    deleteParticipant(recordId, studentName) {
        const message = studentName
            ? `Удалить участника «${studentName}»? Будет удалена запись о соревновании вместе с результатом и вложениями — действие необратимо.`
            : "Удалить участника? Действие необратимо.";
        if (!confirm(message)) {
            return;
        }
        this.request(`/competition/${recordId}/delete`, {
            body: new FormData(),
            onSuccess: () => window.location.reload(),
            onError: (message2) => alert(message2 || "Ошибка удаления записи"),
        });
    }
}

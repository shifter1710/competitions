class Main {
    constructor() {
        this.contentWrapper = document.querySelector(".content-wrapper");
        this.importForm = document.querySelector(".import-form");
        this.manualForm = document.querySelector(".manual-form");
        this.manualFormButton = document.querySelector(".manual-form__button");
        this.manualFormCancelButton = document.querySelector(".manual-form__cancel-button");
        this.manualDateInput = document.querySelector('.manual-form [name="date"]');
        this.reportForm = document.querySelector(".filter-form");
        this.fileInput = document.querySelector(".import-form__input");
        this.importButton = document.querySelector(".import-form__button");

        this.loader = document.querySelector(".loader");

        this.inputName = document.querySelector(".filter__name");
        this.dateFromInput = document.querySelector(".filter__date-from");
        this.dateToInput = document.querySelector(".filter__date-to");
        this.levelSelectWrapper = document.querySelector(".filter__level-wrapper")
        this.positionSelectWrapper = document.querySelector(".filter__position-wrapper")
        this.filterButton = document.querySelector(".filter__button");
        this.cleanFilterButton = document.querySelector(".filter-form__clean-button");
        this.cleanButton = document.querySelector(".clean-button");
        this.exportButton = document.querySelector(".export-button");
        this.dateRangePickerElement = document.querySelector('.datetime');

        this.filterIsApplied = false;
        this.createInstances();
        this.hangEvents();
    }

    createInstances() {
        this.dateRangePicker = new DateRangePicker(this.dateRangePickerElement, {
            format: "dd.mm.yyyy"
        });
        if (this.manualDateInput) {
            this.manualDatePicker = new Datepicker(this.manualDateInput, {
                autohide: true,
                format: "dd.mm.yyyy"
            });
        }
        this.levelSelect = NiceSelect.bind(document.querySelector(".filter__level"), {searchable: true, searchtext: "Найти"});
        this.positionSelect = NiceSelect.bind(document.querySelector(".filter__position"), {searchable: true, searchtext: "Найти"});
    }

    destroyInstances() {
        this.levelSelect.destroy();
        this.positionSelect.destroy();
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
            this.manualDateInput.addEventListener("blur", () => this.normalizeManualDateInput());
        }
        if (this.fileInput) {
            this.fileInput.addEventListener("change", () => this.setDisabledImportButton(false))
        }
        this.reportForm.addEventListener("submit", (event) => this.handleSubmitReportForm(event));
        this.cleanFilterButton.addEventListener("click", () => this.handleResetFilterButton())
        this.exportButton.addEventListener("click", () => this.handleClickExportButton())
        if (this.cleanButton) {
            this.cleanButton.addEventListener("click", () => this.cleanDb());
        }
        this.contentWrapper.addEventListener("click", (event) => this.handleContentWrapperClick(event));
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

    handleClickExportButton() {
        if (this.filterIsApplied) {
            const params = this.prepareParamsForReport();
            return this.makeExportRequest("/export/report?" + params);
        }
        this.makeExportRequest("/export/index");
    }

    handleResetFilterButton() {
        this.resetFilter();
        this.makeRequest({
            url: "/",
            options: {
                method: "GET",
            },
            onSuccess: () => {
                this.filterIsApplied = false;
            }
        })
    }

    resetFilter() {
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

    normalizeManualDateInput() {
        if (!this.manualDateInput) {
            return;
        }
        const digits = this.manualDateInput.value.replace(/\D/g, "").slice(0, 8);
        if (digits.length <= 4) {
            return;
        }

        const day = digits.slice(0, 2);
        const month = digits.slice(2, 4);
        const year = digits.slice(4, 8);
        this.manualDateInput.value = [day, month, year].filter(Boolean).join(".");
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
        Array.from(formData.entries()).forEach(field => {
            const [fieldName, value] = field;
            if (value) {
                params.append(fieldName, value);
            }
        })
        return params.toString();
    }

    handleSubmitReportForm(event) {
        event.preventDefault();
        this.getReport(this.prepareParamsForReport());
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
                this.resetFilter();
                this.filterIsApplied = false;
            },
            onError: (message) => alert(message || "Ошибка импорта")
        })
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
                this.resetFilter();
                this.filterIsApplied = false;
            },
            onError: (message) => alert(message || "Ошибка добавления записи")
        })
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
                this.resetFilter();
                this.filterIsApplied = false;
            },
            onError: (message) => alert(message || "Ошибка удаления записи")
        })
    }

    getReport(params) {
        this.makeRequest({
            url: "/report?" + params,
            options: {
                method: "GET"
            },
            onSuccess: () => this.filterIsApplied = true,
            onError: () => alert("Ошибка применения фильтра. Попробуйте позже")
        })
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
            onSuccess: () => alert("База данных успешно очищена"),
            onError: (message) => alert(message || "Ошибка очистки базы данных")
        })
    }

    makeRequest({ url, options = {}, onSuccess = () => {}, onError = () => {} }) {
        this.setLoading(true)
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
                    throw new Error(message || "Request failed");
                });
            }
            return response.text();
        })
        .then((html) => {
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, "text/html");
            const newContentWrapper = doc.querySelector(".content-wrapper");
            this.contentWrapper.innerHTML = newContentWrapper.innerHTML;
            onSuccess();
        })
        .catch((error) => onError(error.message))
        .finally(() => this.setLoading(false))
    }

    makeExportRequest(url) {
        const linkElement = document.createElement("a");
        linkElement.href = url;
        linkElement.click();
    }

    setLoading(state) {
        if (state) {
            return this.loader.classList.add("loader-active");
        }
        this.loader.classList.remove("loader-active");
    }
}

if (
    document.querySelector(".content-wrapper")
    && document.querySelector(".filter-form")
) {
    new Main();
}

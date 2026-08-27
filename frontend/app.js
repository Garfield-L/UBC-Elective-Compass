"use strict";

// Phase 5: the API is the source of truth for real search, filtering, and
// sorting. This file only collects UI state and renders API responses.
// After the Render service exists, replace only this one value with its HTTPS URL.
const API_BASE_URL = "http://127.0.0.1:8000";
const PAGE_SIZE = 20;
const savedCoursesStorageKey = "ubcElectiveCompassSavedCoursesV2";
const guideSeenKey = "ubcElectiveCompassGuideSeen";

const state = {
  interests: [],
  higherLevel: false,
  highAverage: false,
  query: "",
  sortBy: "highest_average",
  totalResults: 0,
  displayedCourses: [],
};

let interestCategories = [];
let suggestionTimer;
let suggestionController;
let suggestionRequestNumber = 0;
let courseRequestNumber = 0;
let currentGuideStep = 0;

const guideSteps = [
  {
    title: "Welcome to UBC Elective Compass",
    content: "<p>UBC Elective Compass is a student-built course discovery tool for exploring UBC courses through interests, course level, historical course averages, and direct course search.</p><p>It is not an official UBC advising or degree-planning tool.</p>",
  },
  {
    title: "Narrow down your options",
    content: "<p>Use filters to focus your search:</p><ul><li><strong>Interest Selection</strong> lets you choose one or more categories.</li><li><strong>Higher-level courses</strong> shows 300- and 400-level courses.</li><li><strong>High historical average</strong> shows courses with a historical average of 80% or higher.</li></ul>",
  },
  {
    title: "Search when you know the subject",
    content: "<p>Use the course search bar when you already have a course or subject in mind.</p><p>You can enter a full course code, subject, partial entry, or common abbreviation. Suggestions are ranked by the course service.</p>",
  },
  {
    title: "Keep a short list",
    content: "<p>Use the heart on any course card to save it locally in this browser.</p><p>Your choices appear in <strong>Saved Courses</strong>, where you can remove them whenever you like.</p>",
  },
];

const elements = {
  finderView: document.querySelector("#finderView"),
  savedView: document.querySelector("#savedView"),
  navLinks: document.querySelectorAll("[data-view]"),
  interestButton: document.querySelector("#interestButton"),
  interestButtonText: document.querySelector("#interestButtonText"),
  interestModal: document.querySelector("#interestModal"),
  interestOptions: document.querySelector("#interestOptions"),
  closeInterestModal: document.querySelector("#closeInterestModal"),
  cancelInterestModal: document.querySelector("#cancelInterestModal"),
  applyInterestModal: document.querySelector("#applyInterestModal"),
  higherLevel: document.querySelector("#higherLevel"),
  highAverage: document.querySelector("#highAverage"),
  findCoursesButton: document.querySelector("#findCoursesButton"),
  resetFiltersButton: document.querySelector("#resetFiltersButton"),
  sortSelect: document.querySelector("#sortSelect"),
  courseSearch: document.querySelector("#courseSearch"),
  searchButton: document.querySelector("#searchButton"),
  clearSearchButton: document.querySelector("#clearSearchButton"),
  searchSuggestions: document.querySelector("#searchSuggestions"),
  resultsSummary: document.querySelector("#resultsSummary"),
  courseStatus: document.querySelector("#courseStatus"),
  courseResults: document.querySelector("#courseResults"),
  loadMoreArea: document.querySelector("#loadMoreArea"),
  loadMoreButton: document.querySelector("#loadMoreButton"),
  savedSummary: document.querySelector("#savedSummary"),
  savedCourses: document.querySelector("#savedCourses"),
  emptyTemplate: document.querySelector("#emptyCoursesTemplate"),
  helpButton: document.querySelector("#helpButton"),
  guideModal: document.querySelector("#guideModal"),
  guideProgress: document.querySelector("#guideProgress"),
  guideTitle: document.querySelector("#guideTitle"),
  guideText: document.querySelector("#guideText"),
  closeGuide: document.querySelector("#closeGuide"),
  skipGuide: document.querySelector("#skipGuide"),
  previousGuide: document.querySelector("#previousGuide"),
  nextGuide: document.querySelector("#nextGuide"),
};

function apiRequest(path, body, signal) {
  return fetch(`${API_BASE_URL}${path}`, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  }).then(async (response) => {
    if (!response.ok) {
      const errorBody = await response.json().catch(() => ({}));
      throw new Error(typeof errorBody.detail === "string" ? errorBody.detail : "Unable to reach the course service. Please try again.");
    }
    return response.json();
  });
}

function searchPayload(query, limit, offset) {
  return {
    query,
    interests: state.interests,
    higher_level: state.higherLevel,
    high_gpa: state.highAverage,
    sort_by: state.sortBy,
    limit,
    offset,
  };
}

function setCourseStatus(message = "", kind = "") {
  elements.courseStatus.textContent = message;
  elements.courseStatus.className = `status-message ${kind}`.trim();
  elements.courseStatus.classList.toggle("hidden", !message);
}

function friendlyServiceError(error) {
  if (error instanceof Error && error.message && error.message !== "Failed to fetch") {
    return error.message;
  }
  return "Unable to reach the course service. Please try again.";
}

function setCourseLoading(isLoading, isLoadingMore = false) {
  elements.findCoursesButton.disabled = isLoading;
  elements.loadMoreButton.disabled = isLoading;
  if (isLoading) setCourseStatus(isLoadingMore ? "Loading more courses…" : "Loading courses…", "loading");
}

function getSavedCourses() {
  try {
    const saved = JSON.parse(localStorage.getItem(savedCoursesStorageKey) || "[]");
    return Array.isArray(saved) && saved.every((course) => course && typeof course.course_code === "string") ? saved : [];
  } catch {
    return [];
  }
}

function storeSavedCourses(courses) {
  localStorage.setItem(savedCoursesStorageKey, JSON.stringify(courses));
}

function isSaved(courseCode) {
  return getSavedCourses().some((course) => course.course_code === courseCode);
}

function toggleSavedCourse(course) {
  const savedCourses = getSavedCourses();
  const nextCourses = isSaved(course.course_code)
    ? savedCourses.filter((saved) => saved.course_code !== course.course_code)
    : [...savedCourses, course];
  storeSavedCourses(nextCourses);
  renderCourseResults();
  renderSavedCourses();
}

function createElement(tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function usableAverage(course) {
  return course.grade_status === "grade_found" && Number.isFinite(course.latest_available_average)
    ? course.latest_available_average
    : null;
}

function courseCard(course) {
  const card = createElement("article", "course-card");
  const top = createElement("div", "course-card-top");
  const codeWrapper = document.createElement("div");
  codeWrapper.append(createElement("p", "course-code", course.course_code));
  const saved = isSaved(course.course_code);
  const saveButton = createElement("button", `save-button ${saved ? "saved" : ""}`, saved ? "♥" : "♡");
  saveButton.type = "button";
  saveButton.setAttribute("aria-label", `${saved ? "Remove" : "Save"} ${course.course_code}`);
  saveButton.setAttribute("aria-pressed", String(saved));
  saveButton.addEventListener("click", () => toggleSavedCourse(course));
  top.append(codeWrapper, saveButton);

  const title = createElement("h2", "", course.title || "Untitled course");
  const metadata = createElement("div", "course-meta");
  const firstTag = Array.isArray(course.interest_tags) && course.interest_tags[0] ? course.interest_tags[0] : "Interest unavailable";
  metadata.append(createElement("span", "tag", firstTag), createElement("span", "level-tag", `${course.level}-level`));
  const description = createElement("p", "course-description", course.description || "Course description unavailable.");

  const averagePanel = createElement("div", "average-panel");
  const averageText = document.createElement("div");
  averageText.append(createElement("span", "average-label", "Historical average"));
  const average = usableAverage(course);
  averageText.append(createElement("span", "average-session", average === null ? "Unavailable" : (course.grade_session || "Session unavailable")));
  averagePanel.append(averageText, createElement("strong", "average-value", average === null ? "Unavailable" : `${average.toFixed(1)}%`));

  card.append(top, title, metadata, description, averagePanel);
  return card;
}

function renderEmptyState(container, title, description) {
  const emptyState = elements.emptyTemplate.content.firstElementChild.cloneNode(true);
  emptyState.querySelector("h2").textContent = title;
  emptyState.querySelector("p").textContent = description;
  container.replaceChildren(emptyState);
}

function renderCourseResults() {
  if (!state.displayedCourses.length && !elements.courseStatus.classList.contains("loading")) {
    renderEmptyState(elements.courseResults, "No courses match these filters.", "Try removing one of your filters, then search again.");
  } else {
    elements.courseResults.replaceChildren(...state.displayedCourses.map(courseCard));
  }
  elements.resultsSummary.textContent = `${state.totalResults.toLocaleString()} course${state.totalResults === 1 ? "" : "s"} match your search.`;
  const hasMore = state.displayedCourses.length < state.totalResults;
  elements.loadMoreArea.classList.toggle("hidden", !hasMore);
  elements.loadMoreButton.textContent = "Load more";
}

function renderSavedCourses() {
  const savedCourses = getSavedCourses();
  elements.savedSummary.textContent = savedCourses.length
    ? `${savedCourses.length} course${savedCourses.length === 1 ? "" : "s"} saved in this browser.`
    : "Courses you save stay in this browser.";
  if (!savedCourses.length) {
    renderEmptyState(elements.savedCourses, "No saved courses yet.", "Save courses from Course Finder and they’ll appear here.");
    return;
  }
  elements.savedCourses.replaceChildren(...savedCourses.map(courseCard));
}

async function loadCourses({ append = false } = {}) {
  const requestNumber = ++courseRequestNumber;
  const offset = append ? state.displayedCourses.length : 0;
  setCourseLoading(true, append);
  if (!append) {
    elements.courseResults.replaceChildren();
    elements.loadMoreArea.classList.add("hidden");
  }
  try {
    const response = await apiRequest("/courses/search", searchPayload(state.query, PAGE_SIZE, offset));
    if (requestNumber !== courseRequestNumber) return;
    const existingCodes = new Set(append ? state.displayedCourses.map((course) => course.course_code) : []);
    const newCourses = response.results.filter((course) => !existingCodes.has(course.course_code));
    state.displayedCourses = append ? [...state.displayedCourses, ...newCourses] : newCourses;
    state.totalResults = response.total_results;
    setCourseStatus();
    renderCourseResults();
  } catch (error) {
    if (requestNumber !== courseRequestNumber) return;
    state.displayedCourses = append ? state.displayedCourses : [];
    state.totalResults = append ? state.totalResults : 0;
    setCourseStatus(friendlyServiceError(error), "error");
    renderCourseResults();
  } finally {
    if (requestNumber === courseRequestNumber) setCourseLoading(false);
  }
}

function runNewSearch(query = elements.courseSearch.value) {
  state.query = query.trim();
  elements.courseSearch.value = state.query;
  elements.clearSearchButton.classList.toggle("hidden", !state.query);
  hideSuggestions();
  state.displayedCourses = [];
  state.totalResults = 0;
  return loadCourses();
}

function hideSuggestions() {
  elements.searchSuggestions.classList.add("hidden");
  elements.courseSearch.setAttribute("aria-expanded", "false");
}

function suggestionButton(icon, title, detail, query) {
  const suggestion = createElement("button", "suggestion-option");
  suggestion.type = "button";
  suggestion.setAttribute("role", "option");
  suggestion.append(
    createElement("span", "suggestion-icon", icon),
    (() => {
      const copy = createElement("span", "suggestion-copy");
      copy.append(createElement("span", "suggestion-title", title), createElement("span", "suggestion-detail", detail));
      return copy;
    })(),
  );
  suggestion.addEventListener("click", () => runNewSearch(query));
  return suggestion;
}

async function loadSuggestions(query) {
  const requestNumber = ++suggestionRequestNumber;
  suggestionController?.abort();
  suggestionController = new AbortController();
  try {
    const response = await apiRequest("/courses/search", searchPayload(query, 10, 0), suggestionController.signal);
    if (requestNumber !== suggestionRequestNumber || query !== elements.courseSearch.value.trim()) return;
    const subjects = [...new Set(response.results.map((course) => course.subject))].slice(0, 3);
    const options = [
      ...subjects.map((subject) => suggestionButton("⌕", `${subject} — View matching courses`, "Subject match", subject)),
      ...response.results.slice(0, 7).map((course) => suggestionButton(
        "•",
        `${course.course_code} — ${course.title}`,
        `${course.level}-level · ${(course.interest_tags || ["Interest unavailable"])[0]}`,
        course.course_code,
      )),
    ];
    if (!options.length) options.push(createElement("p", "suggestion-empty", "No matching courses."));
    elements.searchSuggestions.replaceChildren(...options);
    elements.searchSuggestions.classList.remove("hidden");
    elements.courseSearch.setAttribute("aria-expanded", "true");
  } catch (error) {
    if (error.name === "AbortError" || requestNumber !== suggestionRequestNumber) return;
    elements.searchSuggestions.replaceChildren(createElement("p", "suggestion-empty", friendlyServiceError(error)));
    elements.searchSuggestions.classList.remove("hidden");
  }
}

function scheduleSuggestions() {
  const query = elements.courseSearch.value.trim();
  elements.clearSearchButton.classList.toggle("hidden", !query);
  window.clearTimeout(suggestionTimer);
  if (!query) {
    hideSuggestions();
    return;
  }
  suggestionTimer = window.setTimeout(() => loadSuggestions(query), 250);
}

function updateInterestButton() {
  const count = state.interests.length;
  elements.interestButtonText.textContent = count ? `Select interests (${count})` : "Select interests";
}

function renderInterestOptions() {
  elements.interestOptions.replaceChildren(
    ...interestCategories.map((interest) => {
      const selected = state.interests.includes(interest);
      const option = createElement("button", `interest-option ${selected ? "selected" : ""}`, interest);
      option.type = "button";
      option.setAttribute("aria-pressed", String(selected));
      option.addEventListener("click", () => {
        state.interests = selected
          ? state.interests.filter((value) => value !== interest)
          : [...state.interests, interest];
        renderInterestOptions();
      });
      return option;
    }),
  );
}

async function loadInterests() {
  elements.interestButton.disabled = true;
  try {
    const response = await apiRequest("/interests");
    if (!Array.isArray(response.interests)) throw new Error("Unable to load interests. Please try again.");
    interestCategories = response.interests;
    elements.interestButton.disabled = false;
  } catch (error) {
    elements.interestButtonText.textContent = "Interests unavailable";
    setCourseStatus(friendlyServiceError(error), "error");
  }
}

function openInterestModal() {
  if (!interestCategories.length) return;
  renderInterestOptions();
  elements.interestModal.classList.remove("hidden");
  elements.closeInterestModal.focus();
}

function closeInterestModal() {
  elements.interestModal.classList.add("hidden");
  elements.interestButton.focus();
}

function applyInterests() {
  updateInterestButton();
  closeInterestModal();
  runNewSearch();
}

function resetFilters() {
  state.interests = [];
  state.higherLevel = false;
  state.highAverage = false;
  state.query = "";
  state.sortBy = "highest_average";
  elements.higherLevel.checked = false;
  elements.highAverage.checked = false;
  elements.courseSearch.value = "";
  elements.sortSelect.value = state.sortBy;
  elements.clearSearchButton.classList.add("hidden");
  updateInterestButton();
  runNewSearch("");
}

function renderGuide() {
  const step = guideSteps[currentGuideStep];
  elements.guideProgress.textContent = `${currentGuideStep + 1} / ${guideSteps.length}`;
  elements.guideTitle.textContent = step.title;
  elements.guideText.innerHTML = step.content;
  elements.previousGuide.classList.toggle("hidden", currentGuideStep === 0);
  elements.nextGuide.textContent = currentGuideStep === guideSteps.length - 1 ? "Get Started" : "Next";
}

function openGuide() {
  currentGuideStep = 0;
  renderGuide();
  elements.guideModal.classList.remove("hidden");
  elements.closeGuide.focus();
}

function dismissGuide() {
  localStorage.setItem(guideSeenKey, "true");
  elements.guideModal.classList.add("hidden");
}

function moveGuide(direction) {
  if (currentGuideStep === guideSteps.length - 1 && direction > 0) {
    dismissGuide();
    return;
  }
  currentGuideStep = Math.max(0, Math.min(guideSteps.length - 1, currentGuideStep + direction));
  renderGuide();
}

function switchView(viewName) {
  const finderActive = viewName === "finder";
  elements.finderView.classList.toggle("hidden", !finderActive);
  elements.savedView.classList.toggle("hidden", finderActive);
  elements.navLinks.forEach((link) => {
    const active = link.dataset.view === viewName;
    link.classList.toggle("active", active);
    link.toggleAttribute("aria-current", active);
  });
  if (!finderActive) renderSavedCourses();
}

elements.interestButton.addEventListener("click", openInterestModal);
elements.closeInterestModal.addEventListener("click", closeInterestModal);
elements.cancelInterestModal.addEventListener("click", closeInterestModal);
elements.applyInterestModal.addEventListener("click", applyInterests);
elements.interestModal.addEventListener("click", (event) => {
  if (event.target === elements.interestModal) closeInterestModal();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !elements.interestModal.classList.contains("hidden")) closeInterestModal();
  if (event.key === "Escape" && !elements.guideModal.classList.contains("hidden")) dismissGuide();
});
elements.higherLevel.addEventListener("change", () => {
  state.higherLevel = elements.higherLevel.checked;
  runNewSearch();
});
elements.highAverage.addEventListener("change", () => {
  state.highAverage = elements.highAverage.checked;
  runNewSearch();
});
elements.findCoursesButton.addEventListener("click", () => runNewSearch());
elements.resetFiltersButton.addEventListener("click", resetFilters);
elements.sortSelect.addEventListener("change", () => {
  state.sortBy = elements.sortSelect.value;
  runNewSearch();
});
elements.courseSearch.addEventListener("input", scheduleSuggestions);
elements.courseSearch.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    runNewSearch();
  }
});
elements.searchButton.addEventListener("click", () => runNewSearch());
elements.courseSearch.addEventListener("blur", () => window.setTimeout(hideSuggestions, 120));
elements.clearSearchButton.addEventListener("click", () => runNewSearch(""));
elements.loadMoreButton.addEventListener("click", () => loadCourses({ append: true }));
elements.navLinks.forEach((link) => link.addEventListener("click", () => switchView(link.dataset.view)));
elements.helpButton.addEventListener("click", openGuide);
elements.closeGuide.addEventListener("click", dismissGuide);
elements.skipGuide.addEventListener("click", dismissGuide);
elements.previousGuide.addEventListener("click", () => moveGuide(-1));
elements.nextGuide.addEventListener("click", () => moveGuide(1));

async function initialize() {
  updateInterestButton();
  renderSavedCourses();
  await loadInterests();
  await runNewSearch("");
  if (!localStorage.getItem(guideSeenKey)) window.setTimeout(openGuide, 0);
}

initialize();

// BOSS community site: tab switching, copy button, and live GitHub counts.

(function () {
  function select(tabs, attr, active) {
    tabs.forEach(function (tab) {
      tab.setAttribute("aria-selected", tab.getAttribute(attr) === active ? "true" : "false");
    });
  }

  // Mission Control illustration tabs.
  var consoleTabs = Array.prototype.slice.call(document.querySelectorAll(".console .tab"));
  consoleTabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      var pane = tab.getAttribute("data-pane");
      select(consoleTabs, "data-pane", pane);
      document.querySelectorAll(".console .pane").forEach(function (el) {
        el.hidden = el.getAttribute("data-pane-id") !== pane;
      });
      document.querySelectorAll(".console [data-side]").forEach(function (el) {
        el.classList.toggle("on", el.getAttribute("data-side") === pane);
      });
    });
  });

  // HTTP / Terminal / MCP code tabs.
  var surfaceTabs = Array.prototype.slice.call(document.querySelectorAll(".code-tab"));
  surfaceTabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      var surface = tab.getAttribute("data-surface");
      select(surfaceTabs, "data-surface", surface);
      document.querySelectorAll("[data-surface-id]").forEach(function (el) {
        el.hidden = el.getAttribute("data-surface-id") !== surface;
      });
      document.querySelectorAll("[data-surface-note]").forEach(function (el) {
        el.hidden = el.getAttribute("data-surface-note") !== surface;
      });
    });
  });

  // Copy the install commands.
  var copy = document.querySelector("[data-copy]");
  var source = document.querySelector("[data-copy-source]");
  if (copy && source && navigator.clipboard) {
    copy.addEventListener("click", function () {
      navigator.clipboard.writeText(source.textContent.trim()).then(function () {
        copy.textContent = "Copied";
        setTimeout(function () { copy.textContent = "Copy"; }, 1600);
      }, function () {
        copy.textContent = "Select and copy";
      });
    });
  } else if (copy) {
    copy.hidden = true;
  }

  // Stars and forks, shown only when GitHub answers.
  fetch("https://api.github.com/repos/seanventures/boss")
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (repo) {
      if (!repo || typeof repo.stargazers_count !== "number") return;
      var stars = repo.stargazers_count.toLocaleString();
      var forks = repo.forks_count.toLocaleString();
      document.querySelectorAll("[data-stars]").forEach(function (el) { el.textContent = stars; });
      document.querySelectorAll("[data-forks]").forEach(function (el) { el.textContent = forks; });
      document.querySelectorAll("[data-stat]").forEach(function (el) { el.hidden = false; });
    })
    .catch(function () {});
})();

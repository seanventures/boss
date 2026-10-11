// Local presentation only; product work starts in the installed BOSS console.
(function () {
  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if ("IntersectionObserver" in window && !reducedMotion) {
    document.documentElement.classList.add("js");
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("shown");
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.08 });
    document.querySelectorAll("[data-reveal]").forEach(function (element) { observer.observe(element); });
  }
  var copy = document.querySelector("[data-copy]");
  var source = document.querySelector("[data-copy-source]");
  var status = document.querySelector("[data-copy-status]");
  if (copy && source && navigator.clipboard) {
    copy.addEventListener("click", function () {
      navigator.clipboard.writeText(source.textContent.trim()).then(function () {
        status.textContent = "Commands copied";
      }, function () {
        status.textContent = "Select the commands and copy them";
      });
    });
  } else if (copy) {
    copy.hidden = true;
  }
})();

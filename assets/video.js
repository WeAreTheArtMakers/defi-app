// Click-to-load YouTube: nothing is requested from YouTube until the visitor presses play.
document.querySelectorAll("a.yt[data-yt]").forEach(a => a.addEventListener("click", e => {
  e.preventDefault();
  const f = document.createElement("iframe");
  f.src = "https://www.youtube-nocookie.com/embed/" + encodeURIComponent(a.dataset.yt) + "?rel=0&modestbranding=1&playsinline=1&autoplay=1";
  f.title = a.getAttribute("aria-label") || "Video";
  f.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
  f.allowFullscreen = true;
  f.referrerPolicy = "strict-origin-when-cross-origin";
  f.className = "yt-frame";
  a.replaceWith(f);
  f.focus();
}));

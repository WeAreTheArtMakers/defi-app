// Optional videos that stay hidden until an id is set: <figure class="demo-video" data-video-id="" hidden> (exit-check page).
document.querySelectorAll("figure.demo-video[data-video-id]").forEach(fig => {
  const id = (fig.dataset.videoId || "").trim();
  const a = fig.querySelector("a.yt");
  if (!a || !/^[A-Za-z0-9_-]{11}$/.test(id)) return;
  a.dataset.yt = id;
  a.href = "https://youtu.be/" + id;
  fig.hidden = false;
});
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

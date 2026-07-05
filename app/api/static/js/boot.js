loadGrandTotal(); loadTagChips();
$("pb-vidpref").classList.toggle("on", state.preferVideos);
switchView("home");   // sets state.view, hides Library-only header controls, starts polling
restorePlayer();      // hero survives a refresh: same queue + position, paused
refreshBadges();   // populate the Review + Matches tab badges on boot

# Android Control Navigation Guide

## Choosing a tool
- multi-step goal on a phone: droidclaw_run with a natural-language goal (it observes, acts and verifies on its own)
- a few precise steps you want to drive yourself: adb_screen to read the screen, then adb_tools to act

## Reading the screen (adb_screen)
- mode xml lists elements in reading order: `[7] button "Wi-Fi" id=title at (540,448) checked`
  - number, kind (button, input, toggle, image, list, tappable, text), label, short resource id, center in device pixels, state flags
- mode annotated also saves a screenshot with every listed element boxed and labelled with the same number; pass its path to vision_load and ask about it (e.g. "which number is the search icon?")
- the numbers stay valid until the screen changes; re-read after every action that changes the screen

## Acting (adb_tools)
- prefer {"action": "tap", "element": 7} over coordinates; element numbers map to the exact element center
- coordinates are device pixels ("x,y"); only use them for things without a number (web pages, games, images)
- swipe direction is the finger direction: swipe up scrolls content down / opens the app drawer from home
- launch apps by package name (e.g. com.android.settings) instead of hunting for icons
- type only works for ASCII text; tap the input field first, press enter to submit

## When to look at the image
- the list is short, empty or full of unlabelled icons (web views, games, maps, custom-drawn apps)
- an action did not change the screen and you cannot tell why
- you need visual state the list cannot express (highlighted tab, colours, which image is which)

## Pitfalls
- the soft keyboard hides content: press back to dismiss it before tapping things under it
- wait a moment after screen transitions; dumps taken mid-animation can be stale or empty
- if two attempts change nothing, change approach (scroll, go back, search, or launch directly)

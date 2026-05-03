# Android Control Navigation Guide

## Reading SCREEN_CONTEXT

When you use screen dump tools, the output is an XML representation of the Android UI hierarchy.
Each element is numbered (indexed) for easy reference. Here is the format:

```
[0] node:
    text: "Settings"
    desc: ""
    bounds: [0,0][1080,176]
    click: true
    id: com.android.settings:id/title

[1] node:
    text: "Wi-Fi"
    desc: "Wi-Fi settings"
    bounds: [0,176][1080,352]
    click: true
    id: com.android.settings:id/title

[2] node:
    text: "Connected"
    desc: ""
    bounds: [0,352][1080,528]
    click: false
    id: com.android.settings:id/summary
```

Key fields:
- **text**: The visible text displayed on screen
- **desc**: Accessibility content description (may be empty)
- **bounds**: Screen coordinates as [x1,y1][x2,y2] (top-left to bottom-right corner)
- **click**: Whether the element is clickable (true/false)
- **id**: The Android resource-id for the element

## Navigation Best Practices

### Tapping Elements
- Always calculate the **center point** of element bounds
- Center formula: `x = (x1 + x2) // 2`, `y = (y1 + y2) // 2`
- Tap the center, not the edges, for reliable hit detection
- Example: bounds [0,176][1080,352] -> tap at (540, 264)

### Scrolling
- Use directional scroll for lists: up, down, left, right
- Scroll distance is proportional to the container size
- Wait for scroll animation to complete before re-dumping
- For long lists, consider using `scrollTo` with text matching

### Text Input
- Type text character by character for maximum reliability
- Use `adb shell input text` for simple ASCII strings
- For special characters, use keyevent codes
- Clear existing text first by long-pressing and selecting all

### Timing
- Wait 500ms-1000ms between actions for UI to update
- After screen transitions, wait longer (1-2 seconds)
- Animations may cause stale element data if you dump too quickly
- Use explicit waits when possible rather than fixed delays

## When to Use Vision vs XML-Only

### XML-Only (Preferred when possible)
- Native Android apps using standard widgets
- Material Design components
- Settings, lists, forms, dialogs
- Apps with good accessibility trees
- Most system apps and well-built third-party apps

### Vision/Screenshot Required
- Games with custom rendering (Unity, Unreal, OpenGL)
- Apps with canvas-based or custom-drawn UI
- Maps and image-heavy applications
- On-screen overlays and floating widgets
- When XML dump returns empty or unhelpful data

### Hybrid Approach
- Start with XML dump for speed and precision
- Fall back to vision/screenshot when:
  - XML dump is empty or has very few nodes
  - Elements you can see on screen are missing from XML
  - Tap coordinates from XML do not hit the intended target
  - The app uses a custom rendering engine

## Common Pitfalls

### Narrow Sidebars
- Navigation drawers and sidebars have narrow bounds
- Coordinates from different sidebar items may overlap
- Solution: Add a horizontal offset (e.g., +100 pixels from left edge)
- Always tap closer to the center of the narrow dimension

### Custom UI Renderers
- Some apps use custom drawing that bypasses the accessibility layer
- XML dump may return an empty or minimal tree
- Solution: Switch to vision/screenshot mode for these apps
- Examples: Most games, drawing apps, camera apps

### WebViews
- WebView content has limited accessibility tree exposure
- You may see only the top-level WebView container in XML
- Inner HTML elements may not appear in the dump
- Solution: Use vision mode or try injecting JavaScript via adb

### Keyboard Covering Elements
- The soft keyboard overlays screen content
- Elements behind the keyboard are still in XML but not visible
- Solution: Dismiss keyboard first (press back or tap outside input)
- Alternatively, scroll the page up to reveal hidden elements

### Animation Delays
- Screen transitions, list scrolling, and dialog animations take time
- Dumping during animation gives stale or partial data
- Solution: Wait 1-2 seconds after transitions before dumping
- For loading spinners, wait until they disappear

### Scrolling Too Fast
- Rapid successive scrolls can skip content
- Lazy-loaded lists need time to populate after scroll
- Solution: Dump screen after each scroll to check current position
- Use smaller scroll distances for precise positioning

## Example Workflows

### Example 1: Open Settings and Toggle Wi-Fi

```
Step 1: Dump screen
  -> See home screen with app icons

Step 2: Find "Settings" icon
  -> [5] node: text="Settings" bounds:[432,1100][648,1296] click:true

Step 3: Tap center of Settings icon
  -> Tap (540, 1198)

Step 4: Wait 1 second, dump screen
  -> Settings menu is visible

Step 5: Find "Wi-Fi" item
  -> [3] node: text="Wi-Fi" bounds:[0,376][1080,520] click:true

Step 6: Tap center of Wi-Fi item
  -> Tap (540, 448)

Step 7: Wait, dump screen
  -> Wi-Fi toggle is visible

Step 8: Find toggle and tap its center
  -> Toggle state changed
```

### Example 2: Search for an App in App Drawer

```
Step 1: Go to home screen (press HOME key)

Step 2: Swipe up to open app drawer
  -> Scroll direction: up from bottom of screen

Step 3: Wait, dump screen
  -> App drawer with search bar visible

Step 4: Tap the search bar
  -> [0] node: text="Search apps" bounds:[32,100][1048,200] click:true
  -> Tap (540, 150)

Step 5: Type the app name character by character
  -> Type "Chrome"

Step 6: Wait for results, dump screen
  -> Search results appear

Step 7: Tap the matching app icon
  -> App launches
```

### Example 3: Navigate a Scrollable List

```
Step 1: Dump screen to see current list position
  -> List items visible with bounds

Step 2: Identify target item in visible area
  -> If found, tap its center and done
  -> If not found, proceed to scroll

Step 3: Scroll down
  -> Wait for scroll to settle

Step 4: Dump screen again
  -> New items visible

Step 5: Check for target item
  -> Repeat steps 3-4 until target found or list ends

Step 6: If list ends (last item same as before),
  the target does not exist in this list
```

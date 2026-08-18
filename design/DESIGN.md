---
name: OpsMind Core
colors:
  surface: '#12131a'
  surface-dim: '#12131a'
  surface-bright: '#383940'
  surface-container-lowest: '#0c0e14'
  surface-container-low: '#1a1b22'
  surface-container: '#1e1f26'
  surface-container-high: '#282a31'
  surface-container-highest: '#33343c'
  on-surface: '#e2e1eb'
  on-surface-variant: '#c2c6d6'
  inverse-surface: '#e2e1eb'
  inverse-on-surface: '#2f3037'
  outline: '#8c909f'
  outline-variant: '#424754'
  surface-tint: '#adc6ff'
  primary: '#adc6ff'
  on-primary: '#002e6a'
  primary-container: '#4d8eff'
  on-primary-container: '#00285d'
  inverse-primary: '#005ac2'
  secondary: '#4edea3'
  on-secondary: '#003824'
  secondary-container: '#00a572'
  on-secondary-container: '#00311f'
  tertiary: '#ffb3ad'
  on-tertiary: '#68000a'
  tertiary-container: '#ff5451'
  on-tertiary-container: '#5c0008'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#d8e2ff'
  primary-fixed-dim: '#adc6ff'
  on-primary-fixed: '#001a42'
  on-primary-fixed-variant: '#004395'
  secondary-fixed: '#6ffbbe'
  secondary-fixed-dim: '#4edea3'
  on-secondary-fixed: '#002113'
  on-secondary-fixed-variant: '#005236'
  tertiary-fixed: '#ffdad7'
  tertiary-fixed-dim: '#ffb3ad'
  on-tertiary-fixed: '#410004'
  on-tertiary-fixed-variant: '#930013'
  background: '#12131a'
  on-background: '#e2e1eb'
  surface-variant: '#33343c'
typography:
  display-lg:
    fontFamily: Geist
    fontSize: 48px
    fontWeight: '600'
    lineHeight: '1.1'
    letterSpacing: -0.02em
  headline-md:
    fontFamily: Geist
    fontSize: 32px
    fontWeight: '500'
    lineHeight: '1.2'
    letterSpacing: -0.01em
  headline-sm:
    fontFamily: Geist
    fontSize: 24px
    fontWeight: '500'
    lineHeight: '1.3'
  body-lg:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '400'
    lineHeight: '1.6'
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: '1.5'
  code-md:
    fontFamily: JetBrains Mono
    fontSize: 13px
    fontWeight: '400'
    lineHeight: '1.6'
  label-caps:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: '600'
    lineHeight: '1'
    letterSpacing: 0.05em
rounded:
  sm: 0.125rem
  DEFAULT: 0.25rem
  md: 0.375rem
  lg: 0.5rem
  xl: 0.75rem
  full: 9999px
spacing:
  unit: 4px
  gutter: 24px
  margin: 32px
  container-max: 1440px
---

## Brand & Style
The design system embodies a **Sophisticated Infrastructure** aesthetic, prioritizing clarity, precision, and a sense of calm authority for self-hosted AI system administration. The personality is powerful yet restrained, avoiding the flamboyant trends of consumer AI in favor of a "pro-tool" environment.

The style is a blend of **High-End Minimalism** and **Technical Modernism**. It utilizes deep charcoal canvases, razor-sharp geometry, and a disciplined approach to information density. The goal is to evoke the feeling of a high-performance mission control center where the AI is an invisible, competent partner rather than a flashy assistant.

- **Whitespace:** Generous but intentional, used to create clear visual grouping and focus.
- **Accents:** Used sparingly as functional signals rather than decorative elements.
- **Atmosphere:** Dark, focused, and utilitarian.

## Colors
The palette is rooted in a "Dark-First" philosophy to reduce eye strain during long monitoring sessions and to provide high contrast for technical data.

- **Base Canvas:** Deepest black (#050505) for the main application background.
- **Surfaces:** Dark gray (#111111) used for cards, sidebars, and containers to create subtle depth.
- **Functional Accents:**
    - **Primary (Blue):** Indicates intelligence, AI actions, and information.
    - **Success (Green):** Indicates healthy infrastructure and resolved issues.
    - **Warning (Yellow):** Used for latent issues or attention-required states.
    - **Danger (Red):** Reserved strictly for critical system failures or destructive actions.
- **Typography Colors:** Pure white for primary text; Zinc-400 (#A1A1AA) for secondary metadata and inactive states.

## Typography
The typography system balances modern sans-serif legibility with monospaced technical precision.

- **Primary Sans (Geist/Inter):** Used for the structural UI, navigation, and explanatory text. Geist provides a sharp, technical edge for headings, while Inter ensures maximum readability for body content.
- **Technical Mono (JetBrains Mono):** Used for all system-generated data, including IP addresses, logs, file paths, and terminal output. This distinguishes human-authored content from machine-authored data.
- **Scaling:** On mobile devices, `display-lg` should scale down to 32px to ensure readability without horizontal scrolling.

## Layout & Spacing
The layout follows a strict 4px grid system, ensuring mathematical alignment across all technical components.

- **Grid Model:** A 12-column fluid grid for main dashboards. Sidebars should be fixed-width (typically 240px or 280px) to maintain consistent navigation.
- **Rhythm:** Use increments of 8px (2 units) for component spacing (e.g., gap between buttons) and 16px-24px for layout spacing.
- **Responsive Behavior:** On tablet, the 12-column grid collapses to 6 columns. On mobile, elements stack into a single column with 16px side margins. Large data tables should implement horizontal scrolling with sticky headers/first columns.

## Elevation & Depth
In this dark-themed system, depth is conveyed through **Tonal Layers** and **Subtle Outlines** rather than heavy shadows.

- **Stacking:** The background is #050505. Elevated elements (cards, modals) use #111111. 
- **Borders:** Every container must have a `1px solid rgba(255, 255, 255, 0.1)` border. This "ghost border" technique provides definition in dark mode without adding visual weight.
- **Shadows:** Avoid large ambient shadows. Use a single, tight, dark shadow `(0px 4px 12px rgba(0,0,0,0.5))` only for floating elements like dropdown menus or modals to separate them from the interface layers.

## Shapes
The shape language is "Soft-Technical." Elements use small corner radii to feel modern and accessible, but not overly "bubbly" or friendly.

- **Base Radius (0.25rem):** Standard for buttons, inputs, and small chips.
- **Container Radius (0.5rem):** Used for cards and larger UI modules.
- **Interactive States:** Buttons should maintain their shape but can use a subtle brightness increase on hover to indicate interactivity.

## Components
- **Buttons:** Primary buttons use a solid blue background with white text. Secondary buttons use the ghost border style with no background.
- **Chips / Status Badges:** Use a "dot + label" format. For example, a "Healthy" chip has a 6px green circle followed by "Healthy" in monospaced text.
- **Inputs:** Dark backgrounds (#050505) with subtle borders. Focus states should use a 1px primary blue border with a faint blue outer glow (2px).
- **Cards:** No background blurs; use solid #111111 with the standard 0.1 opacity border. Headers within cards should have a bottom border to separate titles from content.
- **Logs / Terminal:** A dedicated container with a #000000 background, using JetBrains Mono. Use syntax highlighting colors aligned with the functional palette (e.g., error logs in Red).
- **Data Visualizations:** Charts should use thin strokes (1.5px) and avoid fills unless they are low-opacity gradients (0.1) under a line chart.
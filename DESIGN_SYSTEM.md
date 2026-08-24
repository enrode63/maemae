# EDGE Dashboard Design System

## Trading Reports

- Hierarchy: the page title is the single rank-1 element. Aggregate/team KPI cards are rank 2; timestamps, labels, and API state are rank 3.
- Grid: layout spacing uses the 8px scale (`8 / 16 / 24 / 32`). Dashboard outer spacing remains 24px or greater, while dense data rows use 8px internal gaps.
- Typography: primary KPI is 31px, sentiment value is 39px, section headings are 20px, and report body text is 14px with at least 1.5 line height. Metadata is never below 11px in this view.
- Color: neutral surfaces establish hierarchy without color. Green and red remain semantic gain/loss signals and are reinforced by signed values or labels. Body text on dark surfaces targets WCAG AA contrast.
- Interaction: team tabs update the entire report context. Loading, empty, and error states occupy the same region to avoid layout shift; errors provide an explicit retry action.
- Responsive behavior: five KPI cards collapse to three columns below 1050px and one column below 720px. Weekly review, team reports, allocation, and sentiment cards stack on mobile. Horizontal team tabs remain scrollable.
- Data integrity: no sample trading data is rendered. `/api/trading-reports?team={all|scalping|day|swing|longterm}` is treated as read-only; missing or failed responses resolve to an empty/error state.
- Trading safety: this surface is reporting-only and must not expose order entry, execution, approval, or position mutation controls.

## Automated paper trades

- Automatic records use an `AUTO · PAPER` source label and a top rule; browser-owned manual records use `MANUAL`. Source is never encoded by color alone.
- Automatic cards and details are read-only. Edit/delete remain exclusive to local manual records, and no order or approval control is rendered.
- Loading, empty, API-error, and ready states are announced through a polite live region. Empty/error states never substitute sample data.
- The card grid collapses to one column on mobile, controls retain a 44px minimum target, and both manual and automatic cards open with Enter or Space.
- Ensemble signals: team score (0–100), BUY/SELL/NO_TRADE label, threshold, timeframe, breakdown and rationale are read-only. Missing providers resolve to a labeled `PROVIDER 미설정` / `NO_TRADE` state; delayed and error states never fabricate scores.

# Charts

Every figure in this fleet has one job: **after reading it, can someone act
without running another query?** A count of drugs fails that — it gives a number
and sends the reader back for the names.

Load the `dataviz` skill before writing chart code. What follows is the short
list this fleet has actually got wrong, not a substitute for it.

## Axis ranges — the rule that keeps getting broken

**Ticks land on numbers a reader can hold.** `0 / 1,000 / 2,000 / 3,000`, never
`0 / 756 / 1,513 / 2,269`.

The irregular kind is produced by a specific mistake, and it looks reasonable
while you are writing it:

```python
vmax = max(values) * 1.12        # pad the top a little
for i in range(5):               # five gridlines
    label = i / 4 * vmax         # <- 756, 1513, 2269 ...
```

Padding a maximum by an arbitrary factor and slicing it into equal parts
produces arbitrary ticks. Round the ceiling instead, and **choose the tick count
as well as the ceiling** — the two interact:

| max | 4 ticks | 3 ticks |
| --- | --- | --- |
| 2,702 | ceiling 4,000, bar fills 68% | ceiling **3,000**, bar fills **90%** |

Both are round; the second wastes a third less of the panel. Among equally round
options, the tightest wins.

**The rule is about the STEP, not the ceiling.** A ceiling of 12.5 over five
ticks is a round ceiling and a step of 2.5, and renders as
`0 / 2 / 5 / 7 / 10 / 12` — round-looking and unreadable. This shipped once.
Steps are **1, 2 or 5 times a power of ten** and nothing else. `examples/visualize.py:nice_axis()` in
wss-drug-scarcity is the reference implementation.

**Start at zero for anything whose length encodes magnitude** — bars, columns,
areas. A truncated baseline exaggerates differences by an amount the reader
cannot see. Lines showing *change* may start elsewhere if the axis says so
plainly.

**A second axis is legal for a count against a rate.** The dual-axis warning is
real but narrower than it is usually stated. It bites when both series are the
**same kind of quantity** at different magnitudes — users against sessions —
because a reader takes the crossing for an event when it is an artefact of how
the two scales were placed.

A count and a rate are different kinds of thing. Nobody reads the moment a
"% classified OAI" line crosses an "inspections per year" bar as meaningful, and
bars-plus-line is the standard form for a *volume down, rate up* finding.
Splitting it into two panels is also correct and usually worse: the divergence is
the finding, and separate panels make the reader carry a shape across a gap.

Three things make the overlay honest, and all three are load-bearing:

- **both axes start at zero**, so neither is scaled to manufacture a crossing
- **both carry round ticks**, so the alignment is principled rather than whatever
  a padding factor produced
- **each axis is keyed by a swatch beside its title** — never by colouring the
  tick text, which is illegible in a light hue

`examples/charts/inspection-capacity.svg` in wss-drug-scarcity is the worked
example.

**Log scales are opt-in and labelled.** Use one when the data spans orders of
magnitude, say so on the axis, and never mix log and linear panels without
labelling both.

## The crimes this fleet has actually committed

Each of these shipped. They are cheap to check and expensive to leave.

| crime | what it does | fix |
| --- | --- | --- |
| irregular ticks | `756 / 1,513 / 2,269` — unreadable | round the ceiling, pick the tick count |
| two scales for two counts | users against sessions: the crossover looks like an event and is an artefact of scaling | two panels, or index both to a common base |
| a value on every point | unreadable, and nobody reads any of them | direct-label the ends and the extreme; the axis carries the rest |
| a count with no names | "18 drugs are short" sends the reader back for a query | name the entities on the figure |
| partial periods plotted whole | a fiscal year opening 1 October reads as a collapse | exclude partial periods and say so on the figure |
| a gap plotted as a finding | an empty column from an uncaptured year reads as zero | state the capture window on the figure |
| an unvalidated SVG | the renderer prints a success line for a file that will not parse | parse every output before committing |
| "N of M" across two units | `773 of 205 sites` — impossible on its face, because 773 is firm-DAYS and 205 is sites | "of" claims a subset. Only use it where the numerator is drawn from the denominator; otherwise name both units |

The last three are this fleet's own: a recall column empty before 2022 because
the source only covers 2022+; a chart that shipped as invalid XML because a list
was interpolated where a string was expected while the script reported success;
and a label reading "773 of 205 sites", which happened by reusing a label
template between two charts whose numerators had different units. "17 of 90
sites" was true for import alerts, where the numerator is a subset of the
denominator. Refusal firm-days are not a subset of sites, and the format string
carried the claim across anyway.

## Before committing a figure

1. Do the ticks read as round numbers?
2. Does every length-encoding axis start at zero?
3. Is each panel one scale?
4. Are partial periods and capture gaps stated **on the figure**, not only in a
   commit message?
5. Does it parse? `python3 -c "import xml.dom.minidom;xml.dom.minidom.parse(p)"`
6. Can a reader act on it without another query?

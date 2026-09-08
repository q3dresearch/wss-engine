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
options, the tightest wins. `examples/visualize.py:nice_axis()` in
wss-drug-scarcity is the reference implementation.

**Start at zero for anything whose length encodes magnitude** — bars, columns,
areas. A truncated baseline exaggerates differences by an amount the reader
cannot see. Lines showing *change* may start elsewhere if the axis says so
plainly.

**Log scales are opt-in and labelled.** Use one when the data spans orders of
magnitude, say so on the axis, and never mix log and linear panels without
labelling both.

## The crimes this fleet has actually committed

Each of these shipped. They are cheap to check and expensive to leave.

| crime | what it does | fix |
| --- | --- | --- |
| irregular ticks | `756 / 1,513 / 2,269` — unreadable | round the ceiling, pick the tick count |
| dual axis | two scales on one plot invent a crossover that is not in the data | two panels sharing an x-axis, or index both to a common base |
| a value on every point | unreadable, and nobody reads any of them | direct-label the ends and the extreme; the axis carries the rest |
| a count with no names | "18 drugs are short" sends the reader back for a query | name the entities on the figure |
| partial periods plotted whole | a fiscal year opening 1 October reads as a collapse | exclude partial periods and say so on the figure |
| a gap plotted as a finding | an empty column from an uncaptured year reads as zero | state the capture window on the figure |
| an unvalidated SVG | the renderer prints a success line for a file that will not parse | parse every output before committing |

The last two are this fleet's own: a recall column empty before 2022 because the
source only covers 2022+, and a chart that shipped as invalid XML because a list
was interpolated where a string was expected while the script reported success.

## Before committing a figure

1. Do the ticks read as round numbers?
2. Does every length-encoding axis start at zero?
3. Is each panel one scale?
4. Are partial periods and capture gaps stated **on the figure**, not only in a
   commit message?
5. Does it parse? `python3 -c "import xml.dom.minidom;xml.dom.minidom.parse(p)"`
6. Can a reader act on it without another query?

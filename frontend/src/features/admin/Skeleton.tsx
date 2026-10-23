/**
 * Loading placeholders shaped like the content that replaces them, so the page does not jump
 * when the data lands. Marked `aria-busy` and labelled, so a screen reader hears "loading"
 * rather than reading out a wall of empty boxes.
 */

/** One shimmering bar. `w` is any CSS width. */
function Bar({ w = '100%', h }: { w?: string; h?: string }) {
  return <span className="sk" style={{ width: w, ...(h ? { height: h } : {}) }} />
}

export function ClientsTableSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div aria-busy="true" aria-label="Loading clients">
      <table className="clients sk-table">
        <thead>
          <tr>
            <th>Client</th>
            <th>Status</th>
            <th className="num">AI minutes</th>
            <th className="num">Call minutes</th>
            <th className="num">Rates</th>
            <th className="num">Est. this month</th>
            <th className="num">Cost</th>
            <th className="num">Margin</th>
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: rows }, (_, index) => (
            <tr key={index}>
              <td>
                {/* Fixed widths: a percentage would resolve against a column the table has
                    auto-sized to the width of these very bars. */}
                <Bar w="170px" />
                <Bar w="104px" h="0.65rem" />
              </td>
              <td>
                <span className="sk sk-pill" />
                <span className="sk sk-pill" />
              </td>
              <td className="num">
                <Bar w="54px" />
              </td>
              <td className="num">
                <Bar w="54px" />
              </td>
              <td className="num">
                <Bar w="60px" />
              </td>
              <td className="num">
                <Bar w="64px" />
              </td>
              <td className="num">
                <Bar w="52px" />
              </td>
              <td className="num">
                <Bar w="56px" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ClientDetailSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading client">
      <section className="card">
        <Bar w="230px" h="1.15rem" />
        <div className="sk-gap" />
        <Bar w="130px" h="0.7rem" />
        <div className="tiles">
          {Array.from({ length: 5 }, (_, index) => (
            <div className="tile" key={index}>
              <Bar w="68%" h="0.65rem" />
              <div className="sk-gap" />
              <Bar w="80%" h="1.1rem" />
            </div>
          ))}
        </div>
      </section>

      <section className="card">
        <Bar w="110px" h="1rem" />
        <div className="sk-gap" />
        {Array.from({ length: 4 }, (_, index) => (
          <div className="sk-row" key={index}>
            <Bar w="130px" />
            <Bar w="46%" />
          </div>
        ))}
      </section>

      <section className="card">
        <Bar w="90px" h="1rem" />
        <div className="sk-gap" />
        {Array.from({ length: 5 }, (_, index) => (
          <div className="sk-row" key={index}>
            <Bar w="28%" />
            <Bar w="22%" />
            <Bar w="18%" />
          </div>
        ))}
      </section>
    </div>
  )
}

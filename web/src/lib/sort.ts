/** 列表排序共用类型与工具（列键与取值由各页面自定义，交互与提示保持一致）。 */

export interface SortState<K extends string = string> {
  key: K
  dir: 'asc' | 'desc'
}

/** 点击表头后的下一状态：同列翻转升降；换列采用页面给定的默认方向（时间类列通常 desc）。 */
export function nextSort<K extends string>(
  prev: SortState<K>,
  key: K,
  defaultDir: 'asc' | 'desc' = 'asc',
): SortState<K> {
  return prev.key === key
    ? { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' }
    : { key, dir: defaultDir }
}

/** 列值比较：同类型数值直接相减，其余按中文排序；结果按方向取反。 */
export function compareValues(
  a: string | number,
  b: string | number,
  dir: 'asc' | 'desc',
): number {
  const result =
    typeof a === 'number' && typeof b === 'number' ? a - b : String(a).localeCompare(String(b), 'zh')
  return dir === 'asc' ? result : -result
}

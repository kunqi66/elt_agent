<script setup lang="ts">
/**
 * HOCON 只读查看器（fe-04 §6.3，CodeMirror 6）。
 *
 * 为什么是「只读查看器」而不是编辑器：
 * 后端**没有** HOCON 写回接口（`api/pipeline.ts` 只有 fetchHocon/freezeVersion），
 * 放开输入只会让用户改了半天、刷新即丢，还会让人误以为改动进了制品——
 * 那比不让改更危险。真正改 HOCON 的路径是「自动修复」或重新生成。
 * 只读不影响选中与复制，排查问题够用了。
 *
 * 语法高亮用自写的 StreamLanguage tokenizer（不引额外的 HOCON 语法包）：
 * token 名必须是 `@lezer/highlight` 的 tags 导出名，否则解析器会找不到颜色
 * （StreamLanguage 是拿 token 名去 tags 里查表的）。
 *
 * tokenizer 只做词法切分，**不做语法校验**：HOCON 合法性由后端
 * `hocon_parser` 判定，前端即使标错颜色也不影响任何门禁结论。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Compartment, EditorState, RangeSet, RangeSetBuilder, StateEffect, StateField } from '@codemirror/state'
import type { Extension, Range, Text } from '@codemirror/state'
import {
  Decoration,
  EditorView,
  GutterMarker,
  drawSelection,
  dropCursor,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumberMarkers,
  lineNumbers,
} from '@codemirror/view'
import type { DecorationSet } from '@codemirror/view'
import {
  HighlightStyle,
  StreamLanguage,
  foldGutter,
  foldKeymap,
  foldService,
  indentUnit,
  syntaxHighlighting,
} from '@codemirror/language'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { tags } from '@lezer/highlight'
import type { GateSeverity } from '@/types'

/** 与视图侧构造的标记结构对齐（跨组件传递，各自声明） */
interface HoconMark {
  id: string
  line: number
  severity: GateSeverity
}

const props = withDefaults(
  defineProps<{
    content: string
    readonly: boolean
    marks: HoconMark[]
    /** 门禁定位到的行；父级点「定位」时由外部驱动 */
    activeLine: number | null
  }>(),
  { activeLine: null },
)

const emit = defineEmits<{
  change: [content: string]
  'finding-click': [id: string]
  'line-change': [line: number]
}>()

/* ------------------------------------------------------------------ */
/* 词法高亮                                                            */
/* ------------------------------------------------------------------ */

interface HoconStreamState {
  /** 三引号字符串可能跨行，跨行时必须把状态带下去 */
  inTripleQuote: boolean
}

/** 键名出现在行首（可带缩进）：`key = value` / `key {` */
const KEY_PATTERN = /^[A-Za-z_][\w.-]*/

const hoconLanguage = StreamLanguage.define<HoconStreamState>({
  name: 'hocon',
  startState: () => ({ inTripleQuote: false }),
  copyState: (state) => ({ inTripleQuote: state.inTripleQuote }),
  token(stream, state) {
    if (state.inTripleQuote) {
      // 跨行字符串：一直吃到收尾的三个引号
      if (stream.skipTo('"""')) {
        stream.match('"""')
        state.inTripleQuote = false
      } else {
        stream.skipToEnd()
      }
      return 'string'
    }

    if (stream.eatSpace()) {
      return null
    }

    if (stream.match('#') || stream.match('//')) {
      stream.skipToEnd()
      return 'lineComment'
    }

    if (stream.match('"""')) {
      state.inTripleQuote = true
      return 'string'
    }

    if (stream.match(/^"(?:[^"\\]|\\.)*"/)) {
      return 'string'
    }

    // include/import 是 HOCON 的保留字，必须先于键名判定，否则会被当成普通键
    if (stream.match(/^(?:include|import)\b/)) {
      return 'keyword'
    }

    // 布尔/空值必须在数字之前判，否则 true 会被吃掉首字母
    if (stream.match(/^(?:true|false|yes|no)\b/i)) {
      return 'bool'
    }
    if (stream.match(/^null\b/i)) {
      return 'null'
    }
    if (stream.match(/^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/)) {
      return 'number'
    }

    if (stream.match(KEY_PATTERN)) {
      // 键名与值的区别只看后面是不是 `=`/`:`/`{`（纯词法判断，不建语法树）
      return /^\s*(?:=|:|\{)/.test(stream.string.slice(stream.pos)) ? 'propertyName' : 'variableName'
    }

    if (stream.match(/^[{}[\]]/)) {
      return 'bracket'
    }

    if (stream.match(/^[=:,]/)) {
      return 'operator'
    }

    if (stream.match(/^\$\{[^}]*\}/)) {
      return 'atom'
    }

    stream.next()
    return null
  },
  languageData: {
    commentTokens: { line: '#' },
    closeBrackets: { brackets: ['{', '[', '"'] },
  },
})

/**
 * 折叠「以 `{` 结尾的行」到匹配的 `}`。
 * 这是纯视觉的启发式（不识别字符串/注释里的花括号），
 * 折错了只会折错一段，不影响任何结论。
 */
const hoconFold = foldService.of((state, from) => {
  const line = state.doc.lineAt(from)
  const open = line.text.lastIndexOf('{')
  if (open < 0 || line.text.slice(0, open).includes('#')) {
    return null
  }
  let depth = 0
  for (let pos = line.from + open; pos < state.doc.length; pos += 1) {
    const ch = state.doc.sliceString(pos, pos + 1)
    if (ch === '{') {
      depth += 1
    } else if (ch === '}') {
      depth -= 1
      if (depth === 0) {
        return { from: line.to, to: pos }
      }
    }
  }
  return null
})

const hoconHighlight = HighlightStyle.define([
  { tag: tags.lineComment, color: 'var(--elt-text-muted)', fontStyle: 'italic' },
  { tag: tags.propertyName, color: 'var(--elt-accent)' },
  { tag: tags.variableName, color: 'var(--elt-text-primary)' },
  { tag: tags.string, color: 'var(--elt-success)' },
  { tag: tags.number, color: 'var(--elt-risk-p2)' },
  { tag: tags.bool, color: 'var(--elt-warn)' },
  { tag: tags.null, color: 'var(--elt-warn)' },
  { tag: tags.bracket, color: 'var(--elt-text-secondary)' },
  { tag: tags.operator, color: 'var(--elt-text-secondary)' },
  { tag: tags.keyword, color: 'var(--elt-risk-p1)' },
  { tag: tags.atom, color: 'var(--elt-risk-p3)' },
])

/* ------------------------------------------------------------------ */
/* 门禁标记（行背景 + 行号槽圆点）                                       */
/* ------------------------------------------------------------------ */

class FindingMarker extends GutterMarker {
  constructor(readonly severity: GateSeverity) {
    super()
  }

  override elementClass = 'cm-finding-marker'

  override eq(other: GutterMarker): boolean {
    return other instanceof FindingMarker && other.severity === this.severity
  }

  override toDOM(): Node {
    const dot = document.createElement('span')
    dot.className = `cm-finding-dot cm-finding-dot--${this.severity}`
    dot.textContent = '●'
    return dot
  }
}

const setMarks = StateEffect.define<HoconMark[]>()
const setActiveLine = StateEffect.define<number | null>()

interface MarkState {
  marks: HoconMark[]
  activeLine: number | null
  decorations: DecorationSet
  markers: RangeSet<GutterMarker>
}

/** 行号超出文档范围时直接丢弃：脏数据不该让编辑器整体崩掉 */
function inRange(line: number, totalLines: number): boolean {
  return Number.isInteger(line) && line >= 1 && line <= totalLines
}

function buildState(doc: Text, marks: HoconMark[], activeLine: number | null): MarkState {
  const decos: Range<Decoration>[] = []
  const markerBuilder = new RangeSetBuilder<GutterMarker>()
  const sorted = marks.filter((m) => inRange(m.line, doc.lines)).sort((a, b) => a.line - b.line)

  for (const m of sorted) {
    const from = doc.line(m.line).from
    markerBuilder.add(from, from, new FindingMarker(m.severity))
    decos.push(Decoration.line({ class: `cm-finding-line cm-finding-line--${m.severity}` }).range(from))
  }

  if (activeLine !== null && inRange(activeLine, doc.lines)) {
    decos.push(Decoration.line({ class: 'cm-active-finding-line' }).range(doc.line(activeLine).from))
  }

  return {
    marks,
    activeLine,
    // 同一行可能既有 finding 又有光标行，Decoration.set 的 sort 参数负责排好序
    decorations: Decoration.set(decos, true),
    markers: markerBuilder.finish(),
  }
}

const markField = StateField.define<MarkState>({
  create: () => ({ marks: [], activeLine: null, decorations: Decoration.none, markers: RangeSet.empty }),
  update(value, tr) {
    let marks = value.marks
    let activeLine = value.activeLine
    let changed = tr.docChanged

    for (const e of tr.effects) {
      if (e.is(setMarks)) {
        marks = e.value
        changed = true
      } else if (e.is(setActiveLine)) {
        activeLine = e.value
        changed = true
      }
    }

    if (!changed) {
      return value
    }
    return buildState(tr.newDoc, marks, activeLine)
  },
})

/* ------------------------------------------------------------------ */
/* 编辑器生命周期                                                       */
/* ------------------------------------------------------------------ */

const host = ref<HTMLDivElement | null>(null)
const view = ref<EditorView | null>(null)
const readOnlyCompartment = new Compartment()

const readOnlyExtensions = computed<Extension[]>(() => [
  EditorState.readOnly.of(props.readonly),
  EditorView.editable.of(!props.readonly),
])

function baseExtensions(): Extension[] {
  return [
    lineNumbers({
      domEventHandlers: {
        // 点行号 = 点该行的 finding：行号槽比文字更难点错
        click: (v, line) => {
          const number = v.state.doc.lineAt(line.from).number
          const hit = props.marks.find((m) => m.line === number)
          if (hit !== undefined) {
            emit('finding-click', hit.id)
            return true
          }
          return false
        },
      },
    }),
    highlightActiveLineGutter(),
    highlightActiveLine(),
    foldGutter(),
    history(),
    drawSelection(),
    dropCursor(),
    indentUnit.of('  '),
    EditorState.tabSize.of(2),
    keymap.of([...defaultKeymap, ...historyKeymap, ...foldKeymap]),
    EditorView.lineWrapping,
    syntaxHighlighting(hoconHighlight),
    hoconLanguage,
    hoconFold,
    // 门禁标记走 StateField + 两个 facet，行装饰与行号槽圆点由同一份状态派生
    markField,
    EditorView.decorations.from(markField, (v) => v.decorations),
    lineNumberMarkers.from(markField, (v) => v.markers),
    EditorView.updateListener.of((update) => {
      if (update.docChanged) {
        emit('change', update.state.doc.toString())
      }
      if (update.selectionSet || update.docChanged) {
        const head = update.state.selection.main.head
        emit('line-change', update.state.doc.lineAt(head).number)
      }
    }),
    EditorView.theme({
      '&': { fontSize: '12px', backgroundColor: 'transparent' },
      '.cm-content': { fontFamily: 'var(--elt-mono)', padding: '6px 0' },
      '.cm-gutters': {
        color: 'var(--elt-text-muted)',
        backgroundColor: 'transparent',
        border: 'none',
      },
      '.cm-activeLine': { backgroundColor: 'rgba(255, 255, 255, 0.03)' },
      '.cm-scroller': { overflow: 'auto' },
    }),
  ]
}

onMounted(() => {
  if (host.value === null) {
    return
  }
  const state = EditorState.create({
    doc: props.content,
    extensions: [...baseExtensions(), readOnlyCompartment.of(readOnlyExtensions.value)],
  })
  const instance = new EditorView({ state, parent: host.value })
  view.value = instance
  instance.dispatch({ effects: setMarks.of(props.marks) })
  if (props.activeLine !== null) {
    scrollToLine(props.activeLine)
  }
})

onBeforeUnmount(() => {
  view.value?.destroy()
  view.value = null
})

/** 后端重取制品后整体换文：只在内容真的不同时替换，避免把用户选区顶掉 */
watch(
  () => props.content,
  (next) => {
    const instance = view.value
    if (instance === null) {
      return
    }
    if (instance.state.doc.toString() === next) {
      return
    }
    instance.dispatch({ changes: { from: 0, to: instance.state.doc.length, insert: next } })
  },
)

watch(
  () => props.readonly,
  () => {
    view.value?.dispatch({ effects: readOnlyCompartment.reconfigure(readOnlyExtensions.value) })
  },
)

watch(
  () => props.marks,
  (next) => {
    view.value?.dispatch({ effects: setMarks.of(next) })
  },
)

function scrollToLine(line: number | null): void {
  const instance = view.value
  if (instance === null || line === null) {
    return
  }
  if (!inRange(line, instance.state.doc.lines)) {
    return
  }
  const pos = instance.state.doc.line(line).from
  instance.dispatch({
    selection: { anchor: pos },
    effects: [
      setActiveLine.of(line),
      EditorView.scrollIntoView(pos, { y: 'center' }),
    ],
  })
}

watch(() => props.activeLine, scrollToLine)
</script>

<template>
  <div ref="host" class="hocon-editor" />
</template>

<style scoped>
.hocon-editor {
  height: 100%;
  min-height: 0;
  overflow: hidden;
  border: 1px solid var(--elt-border-subtle);
  border-radius: var(--elt-radius-card);
}

.hocon-editor :deep(.cm-editor) {
  height: 100%;
}

.hocon-editor :deep(.cm-editor.cm-focused) {
  outline: none;
}

/* 门禁标记：颜色只表达严重度，与全站 severity 色板一致 */
.hocon-editor :deep(.cm-finding-line--block) {
  background-color: var(--elt-badge-danger);
}

.hocon-editor :deep(.cm-finding-line--warn) {
  background-color: rgba(230, 162, 60, 0.14);
}

.hocon-editor :deep(.cm-finding-line--info) {
  background-color: rgba(255, 255, 255, 0.04);
}

.hocon-editor :deep(.cm-active-finding-line) {
  outline: 1px solid var(--elt-accent);
  outline-offset: -1px;
}

.hocon-editor :deep(.cm-finding-marker) {
  padding: 0 2px;
}

.hocon-editor :deep(.cm-finding-dot--block) {
  color: var(--elt-danger);
}

.hocon-editor :deep(.cm-finding-dot--warn) {
  color: var(--elt-warn);
}

.hocon-editor :deep(.cm-finding-dot--info) {
  color: var(--elt-text-muted);
}
</style>

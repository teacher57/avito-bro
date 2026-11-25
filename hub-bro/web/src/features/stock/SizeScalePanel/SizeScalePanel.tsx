import { useState } from "react";
import { Button, Input } from "@/shared/ui";
import { compareSizes, formatSize, isSizeNumber, type SizeLabel } from "@/shared/stock";
import type { ChangeSettings } from "../store";
import styles from "./SizeScalePanel.module.css";

// Шкала размеров магазина (catalog.sizeScale): у каждого номера — буквы «S», «M», «2XS»… Одна на весь магазин:
// по ней хаб пишет «30 (M)», а бот называет размеры покупателям так же. Описание — docs/WAREHOUSE.md, «Шкала размеров».

/** Буквы размера: латиница и цифры, до 4 знаков, заглавными («2XS», «XL»). Пусто — без букв. */
export const normalizeLabel = (s: string) => s.trim().toUpperCase().replace(/[^0-9A-Z]/g, "").slice(0, 4);

type Props = {
  sizeScale: SizeLabel[];
  change: ChangeSettings;
};

export function SizeScalePanel({ sizeScale, change }: Props) {
  const rows = [...sizeScale].sort((a, b) => compareSizes(a.size, b.size));
  const [newSize, setNewSize] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const size = newSize.trim();
  const problem = !size ? null : !isSizeNumber(size) ? "Размер — только число: 30" :
    sizeScale.some((s) => s.size === size) ? "Такой размер уже есть" : null;

  const setLabel = (row: SizeLabel, raw: string) => {
    const label = normalizeLabel(raw);
    if (label === row.label) return;
    change(
      label ? `Размер ${row.size}: буквы «${label}»${row.label ? ` (было «${row.label}»)` : ""}` : `Размер ${row.size}: буквы убраны`,
      (s) => ({ sizeScale: s.sizeScale.map((x) => (x.size === row.size ? { ...x, label } : x)) }),
    );
  };

  const add = () => {
    if (!size || problem) return;
    const label = normalizeLabel(newLabel);
    change(`Размер ${formatSize(size, [{ size, label }])} добавлен в шкалу`, (s) => ({
      sizeScale: [...s.sizeScale, { size, label }].sort((a, b) => compareSizes(a.size, b.size)),
    }));
    setNewSize("");
    setNewLabel("");
  };

  return (
    <div className={styles.panel}>
      <p className={styles.hint}>
        Буквы размера видны везде как «30 (M)» — на складе, в заказах и <b>у бота</b>: он называет размеры покупателям
        так же. Шкала одна на весь магазин. Новые размеры попадают сюда сами, когда их добавляют модели или поставки, —
        тогда допишите им буквы.
      </p>

      {rows.length === 0 ? (
        <p className={styles.hint}>Размеров пока нет — они появятся с первой моделью или поставкой.</p>
      ) : (
        <div className={styles.list}>
          {rows.map((row) => (
            <label key={row.size} className={styles.row}>
              <span className={styles.size}>{row.size}</span>
              <input
                key={row.label /* после сохранения поле показывает сохранённое */}
                className={styles.label}
                defaultValue={row.label}
                placeholder="—"
                maxLength={6}
                aria-label={`Буквы размера ${row.size}`}
                onBlur={(e) => setLabel(row, e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                }}
              />
              <span className={styles.preview}>{formatSize(row.size, [row])}</span>
            </label>
          ))}
        </div>
      )}

      <div className={styles.addForm}>
        <Input label="Новый размер" value={newSize} onChange={(e) => setNewSize(e.target.value)} placeholder="35" inputMode="numeric" />
        <Input label="Буквы" value={newLabel} onChange={(e) => setNewLabel(e.target.value)} placeholder="XL" />
        <Button onClick={add} disabled={!size || !!problem}>
          Добавить размер
        </Button>
        {problem && <span className={styles.problem}>{problem}</span>}
      </div>
    </div>
  );
}

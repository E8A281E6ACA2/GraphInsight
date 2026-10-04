import { useState, useEffect } from 'react';
import { Box, Typography, Paper, Chip } from '@mui/material';
import { getNodeLabel } from '../../utils/colorMapping';

interface NodeTooltipProps {
  visible: boolean;
  x: number;
  y: number;
  nodeData: {
    id: string;
    label: string;
    type: string;
    properties: Record<string, unknown>;
  } | null;
}

const PROPERTY_PREVIEW_KEYS = [
  'name',
  'title',
  'label',
  '名称',
  '标题',
  'block_type',
  'heading_path',
  'page_start',
  'page_end',
  'source_location',
  'chunk_id',
  'doc_id',
  'description',
  'summary',
  'text',
  'content',
  '摘要',
  '正文',
];

const displayText = (value: unknown, maxLength = 120): string => {
  if (Array.isArray(value)) {
    const text = value.map((item) => displayText(item, 32)).filter(Boolean).join(', ');
    return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
  }
  if (typeof value === 'string') {
    const text = value.trim().replace(/\s+/g, ' ');
    return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text;
  }
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return '';
};

const shortenId = (value: string) => {
  if (value.length <= 18) return value;
  return `${value.slice(0, 8)}...${value.slice(-6)}`;
};

const buildPropertyRows = (properties: Record<string, unknown>, label: string) => {
  const rows: Array<{ key: string; value: string }> = [];
  const used = new Set<string>();
  const normalizedLabel = label.trim().toLowerCase();

  const addRow = (key: string, value: unknown, maxLength = 120) => {
    if (used.has(key)) return;
    const text = displayText(value, maxLength);
    if (!text) return;
    if (['name', 'title', 'label', '名称', '标题'].includes(key) && text.toLowerCase() === normalizedLabel) {
      return;
    }
    used.add(key);
    rows.push({ key, value: text });
  };

  PROPERTY_PREVIEW_KEYS.forEach((key) => {
    const isLongText = ['description', 'summary', 'text', 'content', '摘要', '正文'].includes(key);
    addRow(key, properties[key], isLongText ? 180 : 120);
  });

  Object.entries(properties).forEach(([key, value]) => {
    if (rows.length >= 6) return;
    addRow(key, value, 120);
  });

  return rows.slice(0, 6);
};

export function NodeTooltip({ visible, x, y, nodeData }: NodeTooltipProps) {
  const [show, setShow] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => {
      setShow(Boolean(visible && nodeData));
    }, visible && nodeData ? 300 : 0);

    return () => clearTimeout(timer);
  }, [visible, nodeData]);

  const shouldShow = visible && show && nodeData;

  if (!shouldShow) {
    return null;
  }

  // 计算提示框位置（避免超出屏幕）
  const tooltipX = Math.min(x + 12, window.innerWidth - 284);
  const tooltipY = Math.min(y + 12, window.innerHeight - 220);
  const propertyRows = buildPropertyRows(nodeData.properties, nodeData.label);

  return (
    <Paper
      elevation={8}
      sx={{
        position: 'fixed',
        left: tooltipX,
        top: tooltipY,
        zIndex: 9999,
        p: 1.25,
        minWidth: 220,
        maxWidth: 280,
        pointerEvents: 'none',
        wordBreak: 'break-word',
        borderRadius: 1.5,
        border: '1px solid',
        borderColor: 'divider',
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, mb: 0.75 }}>
        <Typography variant="subtitle2" fontWeight={700} sx={{ flex: 1, lineHeight: 1.35 }}>
          {nodeData.label}
        </Typography>
        <Chip
          label={getNodeLabel([nodeData.type])}
          size="small"
          color="primary"
          variant="outlined"
          sx={{ height: 22 }}
        />
      </Box>

      <Typography variant="caption" color="text.secondary" display="block" sx={{ mb: propertyRows.length ? 0.75 : 0 }}>
        ID: {shortenId(nodeData.id)}
      </Typography>

      {propertyRows.length > 0 && (
        <Box sx={{ mt: 1, display: 'grid', gap: 0.5 }}>
          {propertyRows.map((row) => (
            <Box
              key={row.key}
              sx={{
                display: 'grid',
                gridTemplateColumns: '64px minmax(0, 1fr)',
                gap: 0.75,
                alignItems: 'start',
              }}
            >
              <Typography variant="caption" color="text.secondary" sx={{ lineHeight: 1.35 }}>
                {row.key}
              </Typography>
              <Typography variant="caption" color="text.primary" sx={{ lineHeight: 1.35 }}>
                {row.value}
              </Typography>
            </Box>
          ))}
        </Box>
      )}
    </Paper>
  );
}

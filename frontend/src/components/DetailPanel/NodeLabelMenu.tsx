
import {
  Menu,
  MenuItem,
  ListItemIcon,
  ListItemText,
  Divider,
} from '@mui/material';
import {
  Palette as PaletteIcon,
  Visibility as VisibilityIcon,
  VisibilityOff as VisibilityOffIcon,
} from '@mui/icons-material';

interface NodeLabelMenuProps {
  anchorEl: HTMLElement | null;
  open: boolean;
  onClose: () => void;
  onConfigureStyle: () => void;
  onToggleVisibility: () => void;
  isVisible: boolean;
}

export function NodeLabelMenu({
  anchorEl,
  open,
  onClose,
  onConfigureStyle,
  onToggleVisibility,
  isVisible,
}: NodeLabelMenuProps) {
  return (
    <Menu
      anchorEl={anchorEl}
      open={open}
      onClose={onClose}
      anchorOrigin={{
        vertical: 'bottom',
        horizontal: 'left',
      }}
      transformOrigin={{
        vertical: 'top',
        horizontal: 'left',
      }}
    >
      <MenuItem onClick={() => { onConfigureStyle(); onClose(); }}>
        <ListItemIcon>
          <PaletteIcon fontSize="small" />
        </ListItemIcon>
        <ListItemText primary="配置节点样式" />
      </MenuItem>
      
      <Divider />
      
      <MenuItem onClick={() => { onToggleVisibility(); onClose(); }}>
        <ListItemIcon>
          {isVisible ? <VisibilityOffIcon fontSize="small" /> : <VisibilityIcon fontSize="small" />}
        </ListItemIcon>
        <ListItemText primary={isVisible ? '隐藏此类型节点' : '显示此类型节点'} />
      </MenuItem>
    </Menu>
  );
}
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function Icon({ children, ...props }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" {...props}>
      {children}
    </svg>
  );
}

export const Icons = {
  pulse: (props: IconProps) => (
    <Icon {...props}><path d="M3 12h4l2.2-5 4.2 10 2.1-5H21" /></Icon>
  ),
  batches: (props: IconProps) => (
    <Icon {...props}><rect x="4" y="4" width="16" height="16" rx="3" /><path d="M8 9h8M8 13h8M8 17h5" /></Icon>
  ),
  source: (props: IconProps) => (
    <Icon {...props}><path d="M3.5 7.5h6l2-2h9v13h-17z" /><path d="M3.5 10h17" /></Icon>
  ),
  audit: (props: IconProps) => (
    <Icon {...props}><path d="M7 3h10v4H7zM5 5H3v16h18V5h-2M7 12h10M7 16h7" /></Icon>
  ),
  settings: (props: IconProps) => (
    <Icon {...props}><circle cx="12" cy="12" r="3" /><path d="M19 13.5v-3l-2-.7-.7-1.7.9-1.9-2.1-2.1-1.9.9-1.7-.7L10.5 2h-3l-.7 2.3-1.7.7-1.9-.9-2.1 2.1.9 1.9-.7 1.7-2.3.7v3l2.3.7.7 1.7-.9 1.9 2.1 2.1 1.9-.9 1.7.7.7 2.3h3l.7-2.3 1.7-.7 1.9.9 2.1-2.1-.9-1.9.7-1.7z" transform="translate(3 0) scale(.75 1)" /></Icon>
  ),
  help: (props: IconProps) => (
    <Icon {...props}><circle cx="12" cy="12" r="9" /><path d="M9.7 9a2.4 2.4 0 1 1 3.2 2.3c-.7.3-.9.8-.9 1.7M12 17h.01" /></Icon>
  ),
  phone: (props: IconProps) => (
    <Icon {...props}><rect x="7" y="2.5" width="10" height="19" rx="2.5" /><path d="M10 5h4M11 18.5h2" /></Icon>
  ),
  battery: (props: IconProps) => (
    <Icon {...props}><rect x="3" y="7" width="17" height="10" rx="2" /><path d="M22 10v4M6 10v4h8v-4z" /></Icon>
  ),
  storage: (props: IconProps) => (
    <Icon {...props}><ellipse cx="12" cy="6" rx="8" ry="3" /><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></Icon>
  ),
  network: (props: IconProps) => (
    <Icon {...props}><path d="M4 9a12 12 0 0 1 16 0M7 12.5a7.5 7.5 0 0 1 10 0M10 16a3 3 0 0 1 4 0" /><circle cx="12" cy="19" r=".7" fill="currentColor" /></Icon>
  ),
  temperature: (props: IconProps) => (
    <Icon {...props}><path d="M10 14.8V5a2 2 0 1 1 4 0v9.8a4 4 0 1 1-4 0z" /><path d="M12 8v9" /></Icon>
  ),
  photos: (props: IconProps) => (
    <Icon {...props}><path d="M12 11V3a4 4 0 0 1 0 8zM13 12h8a4 4 0 0 1-8 0zM12 13v8a4 4 0 0 1 0-8zM11 12H3a4 4 0 0 1 8 0z" /></Icon>
  ),
  refresh: (props: IconProps) => (
    <Icon {...props}><path d="M20 7v5h-5M4 17v-5h5" /><path d="M18.3 9A7 7 0 0 0 6 6.5L4 9M5.7 15A7 7 0 0 0 18 17.5l2-2.5" /></Icon>
  ),
  upload: (props: IconProps) => (
    <Icon {...props}><path d="M12 16V4M7 9l5-5 5 5M4 20h16" /></Icon>
  ),
  install: (props: IconProps) => (
    <Icon {...props}><path d="M12 3v12M7 10l5 5 5-5M4 20h16" /></Icon>
  ),
  bell: (props: IconProps) => (
    <Icon {...props}><path d="M18 9a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" /></Icon>
  ),
  logout: (props: IconProps) => (
    <Icon {...props}><path d="M10 5H4v14h6M14 8l4 4-4 4M8 12h10" /></Icon>
  ),
  power: (props: IconProps) => (
    <Icon {...props}><path d="M12 2v10M6.3 5.7a8 8 0 1 0 11.4 0" /></Icon>
  ),
  fullscreen: (props: IconProps) => (
    <Icon {...props}><path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5" /></Icon>
  ),
  fullscreenExit: (props: IconProps) => (
    <Icon {...props}><path d="M3 8h5V3M21 8h-5V3M3 16h5v5M21 16h-5v5" /></Icon>
  ),
  check: (props: IconProps) => (
    <Icon {...props}><path d="m5 12 4 4L19 6" /></Icon>
  ),
  warning: (props: IconProps) => (
    <Icon {...props}><path d="M12 3 2 21h20zM12 9v5M12 17.5v.5" /></Icon>
  ),
  close: (props: IconProps) => (
    <Icon {...props}><path d="m6 6 12 12M18 6 6 18" /></Icon>
  )
};

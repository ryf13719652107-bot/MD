import { ReactNode } from 'react';
import { Navigate } from 'react-router-dom';
import { useAuthStore } from '../../store/authStore';
import type { UiRole } from '../../store/authStore';

export default function RoleRoute({
  allowedRoles,
  children,
}: {
  allowedRoles: UiRole[];
  children: ReactNode;
}) {
  const role = useAuthStore((s) => s.role);
  if (role == null) return null;
  if (!allowedRoles.includes(role)) {
    return <Navigate to="/" replace />;
  }
  return <>{children}</>;
}

// The Auth context object and its consumer hook, split out from AuthContext.jsx
// so that file exports only the AuthProvider component (Fast Refresh requires a
// component module to export components only).
import { createContext, useContext } from 'react';

export const AuthContext = createContext(null);

export const useAuth = () => useContext(AuthContext);

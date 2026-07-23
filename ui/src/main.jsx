import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import './index.css'
import { ThemeProvider } from './styles/theme'
import { AuthProvider } from './auth/AuthProvider'
import { handleOAuthCallbackRedirect } from './auth/keycloak'
import { APP_BASENAME } from './basePath'

handleOAuthCallbackRedirect()

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ThemeProvider>
      <BrowserRouter basename={APP_BASENAME || undefined}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </ThemeProvider>
  </React.StrictMode>,
)

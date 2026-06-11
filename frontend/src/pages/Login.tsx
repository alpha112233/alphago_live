import { Eye, EyeOff, Info, Loader2, LogIn } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { showToast } from '@/utils/toast'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuthStore } from '@/stores/authStore'

export default function Login() {
  const navigate = useNavigate()
  const { login: setLogin } = useAuthStore()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [isLoading, setIsLoading] = useState(false)
  const [isCheckingSetup, setIsCheckingSetup] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [googleEnabled, setGoogleEnabled] = useState(false)
  const [searchParams] = useSearchParams()

  // Surface errors bounced back from the Google SSO flow (?sso_error=...)
  useEffect(() => {
    const ssoError = searchParams.get('sso_error')
    if (ssoError) setError(ssoError)
  }, [searchParams])

  // Show the Google button only when this instance is SSO-wired
  // (SSO_BROKER_URL + SSO_INSTANCE_ID + SSO_JWT_PUBLIC_KEY_B64 set).
  useEffect(() => {
    fetch('/auth/sso-config', { credentials: 'include' })
      .then((r) => r.json())
      .then((d) => setGoogleEnabled(Boolean(d.google_enabled)))
      .catch(() => {})
  }, [])

  // Check if setup is required or already logged in on page load
  useEffect(() => {
    const checkSetup = async () => {
      try {
        // First check if setup is needed
        const setupResponse = await fetch('/auth/check-setup', {
          credentials: 'include',
        })
        const setupData = await setupResponse.json()
        if (setupData.needs_setup) {
          navigate('/setup', { replace: true })
          return
        }

        // Check if already logged in
        const sessionResponse = await fetch('/auth/session-status', {
          credentials: 'include',
        })

        // Only process if response is successful (not 401 etc.)
        if (sessionResponse.ok) {
          const sessionData = await sessionResponse.json()

          if (sessionData.status === 'success' && sessionData.logged_in && sessionData.broker) {
            // Already fully logged in with broker, go to dashboard
            navigate('/dashboard', { replace: true })
            return
          } else if (
            sessionData.status === 'success' &&
            sessionData.authenticated &&
            !sessionData.logged_in
          ) {
            // Logged in but no broker, go to broker selection
            navigate('/broker', { replace: true })
            return
          }
        }
        // If session check fails (401, etc.), just stay on login page
      } catch (err) {
      } finally {
        setIsCheckingSetup(false)
      }
    }
    checkSetup()
  }, [navigate])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setIsLoading(true)
    setError(null)

    try {
      // First, fetch CSRF token
      const csrfResponse = await fetch('/auth/csrf-token', {
        credentials: 'include',
      })

      if (!csrfResponse.ok) {
        setError('Failed to initialize login. Please refresh the page.')
        setIsLoading(false)
        return
      }

      const csrfData = await csrfResponse.json()

      // Create form data with CSRF token (matches original Flask template approach)
      const formData = new FormData()
      formData.append('username', username)
      formData.append('password', password)
      formData.append('csrf_token', csrfData.csrf_token)

      // Use native fetch like the original template
      const response = await fetch('/auth/login', {
        method: 'POST',
        body: formData,
        credentials: 'include',
      })

      // Check content type before parsing
      const contentType = response.headers.get('content-type')
      if (!contentType || !contentType.includes('application/json')) {
        // If redirected to setup page, inform user
        if (response.url.includes('/setup')) {
          setError('Please complete initial setup first.')
          navigate('/setup')
        } else {
          setError('Login failed. Please try again.')
        }
        setIsLoading(false)
        return
      }

      const data = await response.json()

      if (!response.ok || data.status === 'error') {
        setError(data.message || 'Login failed. Please try again.')
        if (data.redirect) {
          navigate(data.redirect)
        }
      } else {
        // Set login state (broker from response if session was resumed, empty otherwise)
        setLogin(username, data.broker || '')
        showToast.success('Login successful', 'system')
        // Use redirect from response if provided, otherwise go to broker
        navigate(data.redirect || '/broker')
      }
    } catch (err) {
      setError('Login failed. Please try again.')
    } finally {
      setIsLoading(false)
    }
  }

  // Show loading while checking setup
  if (isCheckingSetup) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
      </div>
    )
  }

  return (
    <div className="min-h-screen flex items-center justify-center py-8 px-4">
      <div className="container max-w-6xl">
        <div className="flex flex-col lg:flex-row items-center justify-between gap-8 lg:gap-16">
          {/* Login Form - First on mobile */}
          <Card className="w-full max-w-md order-1 lg:order-2 shadow-xl">
            <CardHeader className="text-center">
              <div className="flex justify-center mb-4">
                <img src="/alpha-live-logo.svg" alt="Alpha Live Trading" className="h-20 w-20" />
              </div>
              <CardTitle className="text-2xl">Welcome Back</CardTitle>
              <CardDescription>Sign in to your Alpha Live Trading account</CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleSubmit} className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="username">Username</Label>
                  <Input
                    id="username"
                    type="text"
                    placeholder="Enter your username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    required
                    disabled={isLoading}
                    autoComplete="username"
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="password">Password</Label>
                  <div className="relative">
                    <Input
                      id="password"
                      type={showPassword ? 'text' : 'password'}
                      placeholder="Enter your password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      required
                      disabled={isLoading}
                      autoComplete="current-password"
                      className="pr-10"
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="absolute right-0 top-0 h-full px-3 hover:bg-transparent"
                      onClick={() => setShowPassword(!showPassword)}
                      aria-label={showPassword ? 'Hide password' : 'Show password'}
                    >
                      {showPassword ? (
                        <EyeOff className="h-4 w-4 text-muted-foreground" />
                      ) : (
                        <Eye className="h-4 w-4 text-muted-foreground" />
                      )}
                    </Button>
                  </div>
                  <div className="text-right">
                    <Link
                      to="/reset-password"
                      className="text-sm text-muted-foreground hover:text-primary"
                    >
                      Forgot password?
                    </Link>
                  </div>
                </div>

                {error && (
                  <Alert variant="destructive">
                    <AlertDescription>{error}</AlertDescription>
                  </Alert>
                )}

                <Button type="submit" className="w-full" disabled={isLoading}>
                  {isLoading ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Signing in...
                    </>
                  ) : (
                    <>
                      <LogIn className="mr-2 h-4 w-4" />
                      Sign in
                    </>
                  )}
                </Button>

                {googleEnabled && (
                  <>
                    <div className="relative my-2">
                      <div className="absolute inset-0 flex items-center">
                        <span className="w-full border-t" />
                      </div>
                      <div className="relative flex justify-center text-xs uppercase">
                        <span className="bg-card px-2 text-muted-foreground">or</span>
                      </div>
                    </div>
                    <Button
                      type="button"
                      variant="outline"
                      className="w-full"
                      disabled={isLoading}
                      onClick={() => {
                        window.location.href = '/auth/google/start'
                      }}
                    >
                      <svg className="mr-2 h-4 w-4" viewBox="0 0 24 24" aria-hidden="true">
                        <path
                          fill="#4285F4"
                          d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.27-4.74 3.27-8.1z"
                        />
                        <path
                          fill="#34A853"
                          d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84A11 11 0 0 0 12 23z"
                        />
                        <path
                          fill="#FBBC05"
                          d="M5.84 14.1A6.6 6.6 0 0 1 5.49 12c0-.73.13-1.43.35-2.1V7.06H2.18A11 11 0 0 0 1 12c0 1.77.43 3.45 1.18 4.94l3.66-2.84z"
                        />
                        <path
                          fill="#EA4335"
                          d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1A11 11 0 0 0 2.18 7.06l3.66 2.84C6.71 7.31 9.14 5.38 12 5.38z"
                        />
                      </svg>
                      Continue with Google
                    </Button>
                  </>
                )}
              </form>
            </CardContent>
          </Card>

          {/* Welcome Content - Second on mobile */}
          <div className="flex-1 max-w-xl text-center lg:text-left order-2 lg:order-1">
            <h1 className="text-4xl lg:text-5xl font-bold mb-6">
              Welcome to <span className="text-primary">Alpha Live Trading</span>
            </h1>
            <p className="text-lg lg:text-xl mb-8 text-muted-foreground">
              Sign in to your account to access your trading dashboard and manage your algorithmic
              trading strategies.
            </p>

            <Alert className="mb-6">
              <Info className="h-4 w-4" />
              <AlertTitle>First Time User?</AlertTitle>
              <AlertDescription>
                Contact your administrator to set up your account.
              </AlertDescription>
            </Alert>

            <p className="text-sm text-muted-foreground">
              Dedicated infrastructure · Direct broker connectivity · Your strategies, your control
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}

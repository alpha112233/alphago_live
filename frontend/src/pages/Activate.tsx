import { CheckCircle2, Eye, EyeOff, KeyRound, Loader2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { showToast } from '@/utils/toast'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

/**
 * One-time account activation: the welcome email links to
 * /activate?token=... — the user sets their own password here.
 * No temp password ever travels by email.
 */
export default function Activate() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const token = searchParams.get('token') || ''

  const [username, setUsername] = useState<string | null>(null)
  const [checking, setChecking] = useState(true)
  const [tokenError, setTokenError] = useState<string | null>(null)
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState(false)

  useEffect(() => {
    const validate = async () => {
      if (!token) {
        setTokenError('This activation link is missing its token. Use the link from your welcome email.')
        setChecking(false)
        return
      }
      try {
        const csrfResponse = await fetch('/auth/csrf-token', { credentials: 'include' })
        const csrfData = await csrfResponse.json()
        const form = new FormData()
        form.append('step', 'validate')
        form.append('token', token)
        form.append('csrf_token', csrfData.csrf_token)
        const response = await fetch('/auth/activate', {
          method: 'POST',
          body: form,
          credentials: 'include',
        })
        const data = await response.json()
        if (response.ok && data.status === 'success') {
          setUsername(data.username)
        } else {
          setTokenError(data.message || 'This activation link is invalid or expired.')
        }
      } catch {
        setTokenError('Could not reach the server. Please try again.')
      } finally {
        setChecking(false)
      }
    }
    validate()
  }, [token])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    if (password !== confirm) {
      setError('Passwords do not match.')
      return
    }
    setSubmitting(true)
    try {
      const csrfResponse = await fetch('/auth/csrf-token', { credentials: 'include' })
      const csrfData = await csrfResponse.json()
      const form = new FormData()
      form.append('step', 'set')
      form.append('token', token)
      form.append('new_password', password)
      form.append('csrf_token', csrfData.csrf_token)
      const response = await fetch('/auth/activate', {
        method: 'POST',
        body: form,
        credentials: 'include',
      })
      const data = await response.json()
      if (response.ok && data.status === 'success') {
        setDone(true)
        showToast.success('Password set — you can sign in now', 'system')
        setTimeout(() => navigate('/login'), 1500)
      } else {
        setError(data.message || 'Could not set the password. Please try again.')
      }
    } catch {
      setError('Could not reach the server. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  if (checking) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
      </div>
    )
  }

  return (
    <div className="min-h-screen flex items-center justify-center py-8 px-4">
      <Card className="w-full max-w-md shadow-xl">
        <CardHeader className="text-center">
          <div className="flex justify-center mb-4">
            <img src="/alpha-live-logo.svg" alt="Alpha Live Trading" className="h-20 w-20" />
          </div>
          <CardTitle className="text-2xl">Activate your server</CardTitle>
          <CardDescription>
            {username
              ? `Set a password for "${username}" to finish activation`
              : 'Account activation'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {tokenError ? (
            <Alert variant="destructive">
              <AlertDescription>{tokenError}</AlertDescription>
            </Alert>
          ) : done ? (
            <Alert>
              <CheckCircle2 className="h-4 w-4" />
              <AlertDescription>
                Password set. Redirecting you to sign in…
              </AlertDescription>
            </Alert>
          ) : (
            <form onSubmit={handleSubmit} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="password">New password</Label>
                <div className="relative">
                  <Input
                    id="password"
                    type={showPassword ? 'text' : 'password'}
                    placeholder="Choose a strong password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                    disabled={submitting}
                    autoComplete="new-password"
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
              </div>

              <div className="space-y-2">
                <Label htmlFor="confirm">Confirm password</Label>
                <Input
                  id="confirm"
                  type={showPassword ? 'text' : 'password'}
                  placeholder="Repeat the password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  required
                  disabled={submitting}
                  autoComplete="new-password"
                />
              </div>

              {error && (
                <Alert variant="destructive">
                  <AlertDescription>{error}</AlertDescription>
                </Alert>
              )}

              <Button type="submit" className="w-full" disabled={submitting}>
                {submitting ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Setting password…
                  </>
                ) : (
                  <>
                    <KeyRound className="mr-2 h-4 w-4" />
                    Set password & activate
                  </>
                )}
              </Button>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

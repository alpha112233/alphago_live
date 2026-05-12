import { BookOpen, Briefcase, ExternalLink, Info, Loader2, Plus } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { activateBroker, listSavedBrokers } from '@/api/brokerManager'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useAuthStore } from '@/stores/authStore'

// All supported brokers with their display names and auth types
const allBrokers = [
  { id: 'fivepaisa', name: '5 Paisa', authType: 'totp' },
  { id: 'fivepaisaxts', name: '5 Paisa (XTS)', authType: 'totp' },
  { id: 'aliceblue', name: 'Alice Blue', authType: 'totp' },
  { id: 'angel', name: 'Angel One', authType: 'totp' },
  { id: 'compositedge', name: 'CompositEdge', authType: 'oauth' },
  { id: 'dhan', name: 'Dhan', authType: 'oauth' },
  { id: 'deltaexchange', name: 'Delta Exchange', authType: 'totp' },
  { id: 'indmoney', name: 'IndMoney', authType: 'totp' },
  { id: 'dhan_sandbox', name: 'Dhan (Sandbox)', authType: 'totp' },
  { id: 'definedge', name: 'Definedge', authType: 'totp' },
  { id: 'firstock', name: 'Firstock', authType: 'totp' },
  { id: 'flattrade', name: 'Flattrade', authType: 'oauth' },
  { id: 'motilal', name: 'Motilal Oswal', authType: 'totp' },
  { id: 'fyers', name: 'Fyers', authType: 'oauth' },
  { id: 'groww', name: 'Groww', authType: 'totp' },
  { id: 'ibulls', name: 'Ibulls', authType: 'totp' },
  { id: 'iifl', name: 'IIFL', authType: 'totp' },
  { id: 'iiflcapital', name: 'IIFL Capital', authType: 'oauth' },
  { id: 'jainamxts', name: 'JainamXts', authType: 'totp' },
  { id: 'kotak', name: 'Kotak Securities', authType: 'totp' },
  { id: 'mstock', name: 'mStock by Mirae Asset', authType: 'totp' },
  { id: 'nubra', name: 'Nubra', authType: 'totp' },
  { id: 'paytm', name: 'Paytm Money', authType: 'oauth' },
  { id: 'pocketful', name: 'Pocketful', authType: 'oauth' },
  { id: 'rmoney', name: 'RMoney', authType: 'oauth' },
  { id: 'samco', name: 'Samco', authType: 'totp' },
  { id: 'shoonya', name: 'Shoonya', authType: 'totp' },
  { id: 'tradejini', name: 'Tradejini', authType: 'totp' },
  { id: 'upstox', name: 'Upstox', authType: 'oauth' },
  { id: 'wisdom', name: 'Wisdom Capital', authType: 'totp' },
  { id: 'zebu', name: 'Zebu', authType: 'totp' },
  { id: 'zerodha', name: 'Zerodha', authType: 'oauth' },
] as const

interface BrokerConfig {
  broker_name: string
  broker_api_key: string
  redirect_url: string
}

// Helper function to get Flattrade API key
function getFlattradeApiKey(fullKey: string): string {
  if (!fullKey) return ''
  const parts = fullKey.split(':::')
  return parts.length > 1 ? parts[1] : fullKey
}

// Generate random state for OAuth
function generateRandomState(): string {
  const length = 16
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  let result = ''
  for (let i = 0; i < length; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length))
  }
  return result
}

export default function BrokerSelect() {
  const { user } = useAuthStore()
  const [selectedBroker, setSelectedBroker] = useState<string>('')
  const [isLoading, setIsLoading] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [brokerConfig, setBrokerConfig] = useState<BrokerConfig | null>(null)
  // alphago_live fork: list of brokers the user has saved credentials for.
  // Drives the dropdown so users can switch between any saved broker.
  const [savedBrokerIds, setSavedBrokerIds] = useState<string[]>([])

  useEffect(() => {
    const loadAll = async () => {
      try {
        const [configRes, savedRes] = await Promise.all([
          fetch('/auth/broker-config', { credentials: 'include' }).then((r) => r.json()),
          listSavedBrokers().catch(() => [] as Awaited<ReturnType<typeof listSavedBrokers>>),
        ])

        if (configRes.status === 'success') {
          setBrokerConfig(configRes)
          const savedIds = savedRes.map((b) => b.broker)
          setSavedBrokerIds(savedIds)
          // Default selection: the currently-active broker if it's in our saved
          // list; otherwise the first saved broker; otherwise blank.
          const initial = savedIds.includes(configRes.broker_name)
            ? configRes.broker_name
            : savedIds[0] || ''
          setSelectedBroker(initial)
        } else {
          setError(configRes.message || 'Failed to load broker configuration')
        }
      } catch {
        setError('Failed to load broker configuration')
      } finally {
        setIsLoading(false)
      }
    }
    loadAll()
  }, [])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()

    if (!selectedBroker) {
      setError('Please select a broker')
      return
    }

    if (!brokerConfig) {
      setError('Broker configuration not loaded')
      return
    }

    setIsSubmitting(true)
    setError(null)
    let cfg: BrokerConfig = brokerConfig

    // alphago_live fork: if the user picked a broker different from the one
    // currently active in .env, activate it first. This rewrites BROKER_API_KEY
    // / BROKER_API_SECRET / REDIRECT_URL on the server so the OAuth redirect
    // below uses the right credentials.
    if (selectedBroker !== cfg.broker_name) {
      try {
        await activateBroker(selectedBroker)
        const r = await fetch('/auth/broker-config', { credentials: 'include' })
        const fresh = await r.json()
        if (fresh.status !== 'success') {
          throw new Error(fresh.message || 'broker-config refresh failed')
        }
        cfg = fresh
        setBrokerConfig(fresh)
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err)
        setError(`Could not switch to ${selectedBroker}: ${msg}`)
        setIsSubmitting(false)
        return
      }
    }

    if (!cfg.broker_api_key) {
      setError(
        `No API key saved for ${selectedBroker}. Open "Manage Brokers" to add credentials first.`
      )
      setIsSubmitting(false)
      return
    }

    let loginUrl = ''
    const { broker_api_key, redirect_url } = cfg

    // Build login URL based on broker type (matching original broker.html logic)
    switch (selectedBroker) {
      case 'fivepaisa':
      case 'fivepaisaxts':
      case 'aliceblue':
      case 'angel':
      case 'mstock':
      case 'indmoney':
      case 'deltaexchange':
      case 'jainamxts':
      case 'dhan_sandbox':
      case 'definedge':
      case 'firstock':
      case 'samco':
      case 'motilal':
      case 'nubra':
      case 'groww':
      case 'ibulls':
      case 'iifl':
      case 'kotak':
      case 'rmoney':
      case 'shoonya':
      case 'tradejini':
      case 'wisdom':
      case 'zebu':
        // Brokers using callback route (form-based or redirect-based)
        loginUrl = `/${selectedBroker}/callback`
        break

      case 'iiflcapital':
        // Route via backend callback endpoint to centralize URL generation and
        // avoid provider-specific redirect parameter parsing differences.
        loginUrl = '/iiflcapital/callback'
        break

      case 'dhan':
        loginUrl = '/dhan/initiate-oauth'
        break

      case 'compositedge':
        loginUrl = `https://xts.compositedge.com/interactive/thirdparty?appKey=${broker_api_key}&returnURL=${redirect_url}`
        break

      case 'flattrade': {
        const flattradeApiKey = getFlattradeApiKey(broker_api_key)
        loginUrl = `https://auth.flattrade.in/?app_key=${flattradeApiKey}`
        break
      }

      case 'fyers':
        loginUrl = `https://api-t1.fyers.in/api/v3/generate-authcode?client_id=${broker_api_key}&redirect_uri=${redirect_url}&response_type=code&state=2e9b44629ebb28226224d09db3ffb47c`
        break

      case 'upstox':
        loginUrl = `https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id=${broker_api_key}&redirect_uri=${redirect_url}`
        break

      case 'zerodha':
        loginUrl = `https://kite.trade/connect/login?api_key=${broker_api_key}`
        break

      case 'paytm':
        loginUrl = `https://login.paytmmoney.com/merchant-login?apiKey=${broker_api_key}&state={default}`
        break

      case 'pocketful': {
        const state = generateRandomState()
        localStorage.setItem('pocketful_oauth_state', state)
        const scope = 'orders holdings'
        loginUrl = `https://trade.pocketful.in/oauth2/auth?client_id=${broker_api_key}&redirect_uri=${redirect_url}&response_type=code&scope=${encodeURIComponent(scope)}&state=${encodeURIComponent(state)}`
        break
      }

      default:
        setError('Please select a broker')
        setIsSubmitting(false)
        return
    }

    // Use setTimeout to ensure state updates complete before navigation
    setTimeout(() => {
      window.location.href = loginUrl
    }, 100)
  }

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin" />
      </div>
    )
  }

  return (
    <div className="min-h-screen flex items-center justify-center py-8 px-4">
      <div className="container max-w-6xl">
        <div className="flex flex-col lg:flex-row items-center justify-between gap-8 lg:gap-16">
          {/* Right side broker form - Shown first on mobile */}
          <Card className="w-full max-w-md shadow-xl order-1 lg:order-2">
            <CardHeader className="text-center">
              <div className="flex justify-center mb-4">
                <img src="/logo.png" alt="Alpha Live Trading" className="h-20 w-20" />
              </div>
              <CardTitle className="text-2xl">Connect Your Trading Account</CardTitle>
              <CardDescription>
                Welcome, <span className="font-medium">{user?.username}</span>!
              </CardDescription>
            </CardHeader>
            <CardContent>
              {error && (
                <Alert variant="destructive" className="mb-4">
                  <AlertDescription>{error}</AlertDescription>
                </Alert>
              )}

              {/* alphago_live: empty state when no brokers have been saved yet */}
              {savedBrokerIds.length === 0 ? (
                <div className="text-center space-y-4 py-4">
                  <Briefcase className="h-10 w-10 mx-auto text-muted-foreground" />
                  <p className="text-sm text-muted-foreground">
                    No brokers configured yet. Add credentials for one or more brokers to start
                    trading.
                  </p>
                  <Link to="/manage-brokers">
                    <Button className="w-full">
                      <Plus className="h-4 w-4 mr-2" />
                      Add your first broker
                    </Button>
                  </Link>
                </div>
              ) : (
                <form onSubmit={handleSubmit} className="space-y-6">
                  <div className="space-y-2">
                    <Label htmlFor="broker-select" className="block text-center">
                      Login with your Broker
                    </Label>
                    <Select
                      value={selectedBroker}
                      onValueChange={setSelectedBroker}
                      disabled={isSubmitting}
                    >
                      <SelectTrigger id="broker-select" className="w-full">
                        <SelectValue placeholder="Select a Broker" />
                      </SelectTrigger>
                      <SelectContent>
                        {allBrokers
                          .filter((broker) => savedBrokerIds.includes(broker.id))
                          .map((broker) => (
                            <SelectItem key={broker.id} value={broker.id}>
                              {broker.name}
                              {broker.id === brokerConfig?.broker_name && (
                                <span className="ml-2 text-xs text-muted-foreground">(active)</span>
                              )}
                            </SelectItem>
                          ))}
                      </SelectContent>
                    </Select>
                  </div>

                  {(selectedBroker === 'zerodha' || selectedBroker === 'dhan') && (
                    <Alert className="border-amber-500/50 bg-amber-500/10">
                      <Info className="h-4 w-4 text-amber-500" />
                      <AlertDescription className="text-amber-200">
                        {selectedBroker === 'zerodha'
                          ? 'Zerodha requires an active Kite Connect data subscription for market data access.'
                          : 'Dhan requires an active Data API subscription for market data access.'}
                      </AlertDescription>
                    </Alert>
                  )}

                  <Button type="submit" className="w-full" disabled={!selectedBroker || isSubmitting}>
                    {isSubmitting ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        Connecting...
                      </>
                    ) : (
                      <>
                        <ExternalLink className="mr-2 h-4 w-4" />
                        Connect Account
                      </>
                    )}
                  </Button>

                  <Link to="/manage-brokers" className="block">
                    <Button type="button" variant="outline" className="w-full">
                      <Briefcase className="h-4 w-4 mr-2" />
                      Manage Brokers
                    </Button>
                  </Link>
                </form>
              )}
            </CardContent>
          </Card>

          {/* Left side content - Shown second on mobile */}
          <div className="flex-1 max-w-xl text-center lg:text-left order-2 lg:order-1">
            <h1 className="text-4xl lg:text-5xl font-bold mb-6">
              Connect Your <span className="text-primary">Broker</span>
            </h1>
            <p className="text-lg lg:text-xl mb-8 text-muted-foreground">
              Link your trading account to start executing trades through Alpha Live Trading's algorithmic
              trading platform.
            </p>

            <Alert className="mb-6">
              <BookOpen className="h-4 w-4" />
              <AlertTitle>Need Help?</AlertTitle>
              <AlertDescription>Check our documentation for broker setup guides.</AlertDescription>
            </Alert>

            <div className="flex justify-center lg:justify-start gap-4">
              <Button variant="outline" asChild>
                <a href="https://docs.openalgo.in" target="_blank" rel="noopener noreferrer">
                  <BookOpen className="mr-2 h-4 w-4" />
                  Documentation
                </a>
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

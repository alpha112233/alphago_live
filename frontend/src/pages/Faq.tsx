import { BookOpen, Download, HelpCircle, Menu, MessageCircle, Moon, Sun } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Footer } from '@/components/layout/Footer'
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Sheet, SheetContent, SheetTrigger } from '@/components/ui/sheet'
import { useThemeStore } from '@/stores/themeStore'

const faqData = [
  {
    category: 'General',
    questions: [
      {
        question: 'What is Alpha Live?',
        answer:
          'Alpha Live is a hosted trading terminal for serious traders. It provides a unified API layer across 24+ Indian brokers and integrates seamlessly with TradingView, Amibroker, Excel, Python, and AI agents, letting you automate your trading strategies without being locked into a single broker.',
      },
      {
        question: 'Which brokers are supported?',
        answer:
          'Alpha Live supports 24+ Indian brokers including Zerodha, Angel One, Dhan, Fyers, ICICI Direct, Kotak Securities, Upstox, 5paisa, Alice Blue, Firstock, Flattrade, IIFL, Jainam, Mastertrust, Motilal Oswal, Nuvama, Paytm Money, Rupeezy, Samco, Shoonya (Finvasia), and more. New brokers are being added regularly.',
      },
      {
        question: 'Do I need to install anything?',
        answer:
          'No. Alpha Live is fully web-based and runs on a dedicated server provisioned for your account. There is nothing to install or maintain — just log in from any modern browser. API access for your own scripts and strategies is available under the API Key page.',
      },
      {
        question: 'Where does my instance run?',
        answer:
          'Each account runs on its own dedicated, managed server with low-latency connectivity to Indian broker APIs. Provisioning, updates, monitoring, and backups are handled for you.',
      },
    ],
  },
  {
    category: 'Costs & Security',
    questions: [
      {
        question: 'What are the costs involved?',
        answer:
          'Alpha Live is offered as a hosted subscription that covers your dedicated server, updates, and support. Standard brokerage charges from your broker still apply. For current plans and pricing, contact admin@alphaquark.in.',
      },
      {
        question: 'How secure is Alpha Live?',
        answer:
          'Security is a top priority. Your broker API credentials are stored encrypted on your dedicated server, which is isolated from other accounts. All communication uses HTTPS, with CSRF protection, rate limiting, and secure session management. We recommend using a strong password and enabling 2FA where available.',
      },
      {
        question: 'Why do I need to login daily?',
        answer:
          'Daily login is required by Indian brokers for security compliance. Broker sessions typically expire at the end of each trading day or after a set period (usually around 3 AM IST). This is a regulatory requirement, not an Alpha Live limitation. The platform makes re-authentication quick and easy with TOTP support for most brokers.',
      },
    ],
  },
  {
    category: 'Features & Integration',
    questions: [
      {
        question: 'Which platforms can I integrate with Alpha Live?',
        answer:
          'Alpha Live integrates with TradingView (via webhooks), Amibroker (via AFL), GoCharting, ChartInk, MetaTrader, Excel, Google Sheets, Python, Node.js, Go, N8N, and any platform that can send HTTP webhooks. You can also use the REST API directly from any programming language.',
      },
      {
        question: 'Does Alpha Live support sandbox trading?',
        answer:
          'Yes! Alpha Live includes an Analyzer/Sandbox mode with sandbox capital of Rs. 1 Crore. This allows you to test strategies in a realistic environment with proper margin calculations, auto square-off at exchange timings, and complete isolation from live trading. Perfect for testing before going live.',
      },
      {
        question: 'Can I run multiple strategies simultaneously?',
        answer:
          'Yes, Alpha Live supports running multiple strategies simultaneously. You can create different webhook endpoints for different strategies, manage them independently, and monitor their performance through the dashboard. The Action Center allows you to control execution modes for each strategy.',
      },
      {
        question: 'Does Alpha Live provide real-time market data?',
        answer:
          'Yes, Alpha Live includes a unified WebSocket server that streams real-time market data from your broker. This data is used for live position tracking, P&L updates, and can be accessed by your strategies. The data is normalized across all brokers for consistent handling.',
      },
    ],
  },
  {
    category: 'Usage & Support',
    questions: [
      {
        question: 'Can I use Alpha Live for my proprietary trading strategies?',
        answer:
          'Yes. Your strategies, configurations, and trading data stay on your dedicated server and are never shared. You are free to run personal or proprietary strategies of any kind.',
      },
      {
        question: 'Can I integrate Alpha Live with GPT/AI assistants?',
        answer:
          'Yes! Alpha Live provides REST APIs that can be called from AI assistants, chatbots, or any automated system. You can build AI-powered trading assistants that use Alpha Live to execute trades based on natural language commands or AI analysis.',
      },
      {
        question: 'How do I get support?',
        answer:
          'Email admin@alphaquark.in for account, billing, or technical support. Step-by-step guides are available at support.alphaquark.in.',
      },
    ],
  },
]

export default function Faq() {
  const { mode, toggleMode } = useThemeStore()
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)

  const navLinks = [
    { href: '/', label: 'Home', internal: true },
    { href: '/faq', label: 'FAQ', internal: true },
  ]

  return (
    <div className="min-h-screen bg-background flex flex-col">
      {/* Navbar */}
      <header className="sticky top-0 z-30 h-16 w-full border-b bg-background/90 backdrop-blur">
        <nav className="container mx-auto px-4 flex h-full items-center justify-between">
          {/* Logo */}
          <div className="flex items-center gap-2">
            {/* Mobile menu button */}
            <Sheet open={mobileMenuOpen} onOpenChange={setMobileMenuOpen}>
              <SheetTrigger asChild className="lg:hidden">
                <Button variant="ghost" size="icon" aria-label="Open menu">
                  <Menu className="h-5 w-5" />
                </Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-80">
                <div className="flex items-center gap-2 mb-8">
                  <img src="/alpha-live-logo.svg" alt="Alpha Live Trading" className="h-8 w-8" />
                  <span className="text-xl font-semibold">Alpha Live Trading</span>
                </div>
                <div className="flex flex-col gap-2">
                  <Link
                    to="/"
                    className="flex items-center gap-2 px-4 py-2 rounded-md hover:bg-accent"
                    onClick={() => setMobileMenuOpen(false)}
                  >
                    <svg
                      xmlns="http://www.w3.org/2000/svg"
                      className="h-5 w-5"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6"
                      />
                    </svg>
                    Home
                  </Link>
                  <Link
                    to="/faq"
                    className="flex items-center gap-2 px-4 py-2 rounded-md hover:bg-accent"
                    onClick={() => setMobileMenuOpen(false)}
                  >
                    <HelpCircle className="h-5 w-5" />
                    FAQ
                  </Link>
                  <Link
                    to="/download"
                    className="flex items-center gap-2 px-4 py-2 rounded-md bg-primary text-primary-foreground hover:bg-primary/90"
                    onClick={() => setMobileMenuOpen(false)}
                  >
                    <Download className="h-5 w-5" />
                    Download
                  </Link>
                </div>
              </SheetContent>
            </Sheet>

            <Link to="/" className="flex items-center gap-2">
              <img src="/alpha-live-logo.svg" alt="Alpha Live Trading" className="h-8 w-8" />
              <span className="text-xl font-bold hidden sm:inline">Alpha Live Trading</span>
            </Link>
          </div>

          {/* Desktop Navigation */}
          <div className="hidden lg:flex items-center gap-1">
            {navLinks.map((link) =>
              link.internal ? (
                <Link key={link.href} to={link.href}>
                  <Button variant="ghost" size="sm">
                    {link.label}
                  </Button>
                </Link>
              ) : (
                <a key={link.href} href={link.href} target="_blank" rel="noopener noreferrer">
                  <Button variant="ghost" size="sm">
                    {link.label}
                  </Button>
                </a>
              )
            )}
          </div>

          {/* Right side */}
          <div className="flex items-center gap-2">
            <Link to="/download">
              <Button size="sm">Download</Button>
            </Link>
            <Button
              variant="ghost"
              size="icon"
              onClick={toggleMode}
              aria-label={mode === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
            >
              {mode === 'dark' ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
            </Button>
          </div>
        </nav>
      </header>

      {/* Main Content */}
      <main className="flex-1">
        <div className="container mx-auto px-4 py-12">
          {/* Header */}
          <div className="text-center mb-12">
            <h1 className="text-4xl lg:text-5xl font-bold mb-4">Frequently Asked Questions</h1>
            <p className="text-lg text-muted-foreground max-w-2xl mx-auto">
              Find answers to common questions about Alpha Live, its features, security, and usage.
            </p>
          </div>

          {/* FAQ Categories */}
          <div className="max-w-4xl mx-auto space-y-8">
            {faqData.map((category) => (
              <Card key={category.category}>
                <CardHeader>
                  <CardTitle>{category.category}</CardTitle>
                  <CardDescription>
                    {category.category === 'General' && 'Basic information about Alpha Live'}
                    {category.category === 'Costs & Security' &&
                      'Pricing, security, and compliance details'}
                    {category.category === 'Features & Integration' &&
                      'Platform capabilities and integrations'}
                    {category.category === 'Usage & Support' &&
                      'Usage guidelines and getting help'}
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <Accordion type="single" collapsible className="w-full">
                    {category.questions.map((faq, index) => (
                      <AccordionItem key={index} value={`${category.category}-${index}`}>
                        <AccordionTrigger className="text-left">{faq.question}</AccordionTrigger>
                        <AccordionContent className="text-muted-foreground">
                          {faq.answer}
                        </AccordionContent>
                      </AccordionItem>
                    ))}
                  </Accordion>
                </CardContent>
              </Card>
            ))}
          </div>

          {/* Resources Section */}
          <div className="max-w-4xl mx-auto mt-16">
            <h2 className="text-2xl font-bold text-center mb-8">Need More Help?</h2>
            <div className="grid md:grid-cols-2 gap-6">
              <Card className="text-center">
                <CardHeader>
                  <BookOpen className="h-10 w-10 mx-auto text-primary" />
                  <CardTitle className="text-lg">Support Docs</CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="text-sm text-muted-foreground mb-4">
                    Step-by-step guides and API references
                  </p>
                  <Button variant="outline" asChild>
                    <a
                      href="https://support.alphaquark.in"
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      Read Docs
                    </a>
                  </Button>
                </CardContent>
              </Card>

              <Card className="text-center">
                <CardHeader>
                  <MessageCircle className="h-10 w-10 mx-auto text-primary" />
                  <CardTitle className="text-lg">Contact Support</CardTitle>
                </CardHeader>
                <CardContent>
                  <p className="text-sm text-muted-foreground mb-4">
                    Questions about your account or setup? We are happy to help.
                  </p>
                  <Button variant="outline" asChild>
                    <a href="mailto:admin@alphaquark.in">Email Support</a>
                  </Button>
                </CardContent>
              </Card>
            </div>
          </div>
        </div>
      </main>

      {/* Footer */}
      <Footer />
    </div>
  )
}

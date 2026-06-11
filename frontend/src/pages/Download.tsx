import { Globe, Key } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

export default function Download() {
  return (
    <div className="container mx-auto px-4 py-8 max-w-4xl">
      <h1 className="text-4xl font-bold text-center mb-8">
        Fully <span className="text-primary">Web-Based</span>
      </h1>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Globe className="h-5 w-5 text-primary" />
            No downloads required
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-muted-foreground">
            Alpha Live is a fully web-based trading terminal. There are no desktop apps or
            installers to download &mdash; your dedicated instance runs in the cloud and is
            accessible from any modern browser, on any device.
          </p>
          <p className="text-muted-foreground">
            For programmatic access from your own scripts and strategies, full REST and WebSocket
            API access is available. Generate your personal key from the API Key page.
          </p>
          <Button asChild>
            <Link to="/apikey">
              <Key className="h-4 w-4 mr-2" />
              Go to API Key
            </Link>
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
